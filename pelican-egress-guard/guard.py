#!/usr/bin/env python3
"""External Grok Build SVG guard.

The guard deliberately lives outside Grok2API.  It authenticates to the
existing admin API, discovers a usable client key, changes only the Build
egress node's proxy *username*, and sends the original probe prompt through
the normal OpenAI-compatible endpoint.  Credentials are read from mounted
files and kept in memory; they are never put in state or event logs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
POLICY_PATH = Path(os.environ.get("GUARD_POLICY_PATH", str(ROOT / "policy.json")))
ANNOTATIONS_PATH = Path(os.environ.get("GUARD_ANNOTATIONS_PATH", str(ROOT / "annotations.json")))
STATE_PATH = Path(os.environ.get("GUARD_STATE_PATH", str(ROOT / "guard-state.json")))
EVENTS_PATH = Path(os.environ.get("GUARD_EVENTS_PATH", str(ROOT / "guard-events.jsonl")))
SAMPLES_PATH = Path(os.environ.get("GUARD_SAMPLES_PATH", str(ROOT / "runtime-samples")))
try:
    MAX_SAVED_SAMPLES = min(5000, max(0, int(os.environ.get("GUARD_MAX_SAVED_SAMPLES", "5000"))))
except ValueError:
    MAX_SAVED_SAMPLES = 5000
GROK_CONFIG_PATH = Path(os.environ.get("GUARD_GROK_CONFIG_PATH", "/run/grok2api/config.yaml"))
RESIN_ENV_PATH = Path(os.environ.get("GUARD_RESIN_ENV_PATH", "/run/secrets/resin.env"))

BASE_URL = os.environ.get("GUARD_BASE_URL", "http://grok2api:8000").rstrip("/")
MODEL = os.environ.get("GUARD_MODEL", "grok-4.5")
NODE_ID = os.environ.get("GUARD_NODE_ID", "").strip()
NODE_NAME = os.environ.get("GUARD_NODE_NAME", "Central Resin Build").strip()
CLIENT_KEY_NAME = os.environ.get("GUARD_CLIENT_KEY_NAME", "").strip()
CLIENT_KEY_PREFIX = os.environ.get("GUARD_CLIENT_KEY_PREFIX", "").strip()
DRY_RUN = os.environ.get("GUARD_DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}
ONESHOT = os.environ.get("GUARD_ONESHOT", "false").lower() in {"1", "true", "yes", "on"}
LOG_LEVEL = os.environ.get("GUARD_LOG_LEVEL", "info").lower()


class GuardError(RuntimeError):
    pass


class APIError(GuardError):
    def __init__(self, status: int, code: str = "api_error") -> None:
        super().__init__(f"api request failed ({status}, {code})")
        self.status = status
        self.code = code


@dataclass
class GenerationResult:
    """The generated SVG plus timing/usage signals from the same request."""

    svg: str
    duration_ms: int
    first_token_ms: int | None = None
    output_tokens: int = 0
    reasoning_tokens: int = 0
    output_tokens_per_second: float | None = None
    response_id: str = ""
    streaming: bool = False


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_log(message: str, **fields: Any) -> None:
    # Never include values that can contain credentials.  Callers pass only
    # ids, labels, timings, and error classes.
    payload = {"ts": now_iso(), "message": message, **fields}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def append_event(event: dict[str, Any]) -> None:
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"ts": now_iso(), **event}, ensure_ascii=False) + "\n")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def yaml_scalar(raw: str) -> str:
    value = raw.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == '"':
            try:
                return str(json.loads(value))
            except json.JSONDecodeError:
                pass
        return value[1:-1]
    return value


def read_bootstrap_admin(path: Path) -> tuple[str, str]:
    """Read only bootstrapAdmin.username/password from the mounted YAML.

    The project config is intentionally simple here.  We avoid adding a YAML
    dependency to this small service and fail closed if either field is absent.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    in_section = False
    values: dict[str, str] = {}
    section_indent = 0
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            in_section = stripped == "bootstrapAdmin:"
            section_indent = indent
            continue
        if in_section and indent <= section_indent:
            in_section = False
        if in_section and ":" in stripped:
            key, raw = stripped.split(":", 1)
            if key in {"username", "password"}:
                values[key] = yaml_scalar(raw)
    username, password = values.get("username", "").strip(), values.get("password", "")
    if not username or not password:
        raise GuardError("bootstrap admin credentials are unavailable")
    return username, password


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def redact_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def unwrap_data(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


def extract_error_code(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
        error = value.get("error", {}) if isinstance(value, dict) else {}
        if isinstance(error, dict):
            return str(error.get("code") or "api_error")[:80]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return "api_error"


def http_json(method: str, path: str, body: Any | None = None, token: str = "", timeout: float = 30.0) -> Any:
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(BASE_URL + path, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(32 * 1024 * 1024)
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read(16 * 1024)
        raise APIError(exc.code, extract_error_code(raw)) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise GuardError(f"http request failed: {type(exc).__name__}") from None


def http_sse(method: str, path: str, body: Any | None = None, token: str = "", timeout: float = 30.0) -> dict[str, Any]:
    """Read a bounded SSE response while preserving timing signals.

    The normal JSON helper buffers a response and therefore cannot observe
    the first generated token.  This helper intentionally keeps only parsed
    event metadata and text needed to recover the SVG; it never logs or
    persists the raw response envelope.
    """

    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "text/event-stream, application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(BASE_URL + path, data=payload, headers=headers, method=method)
    started = time.monotonic()
    events: list[Any] = []
    text_parts: list[str] = []
    first_token_at: float | None = None
    raw_body = bytearray()
    event_name = ""

    def consume_line(line: bytes) -> None:
        nonlocal event_name, first_token_at
        line = line.strip()
        if not line:
            event_name = ""
            return
        if line.startswith(b":"):
            return
        if line.startswith(b"event:"):
            event_name = line[6:].strip().decode("utf-8", errors="replace")
            return
        if not line.startswith(b"data:"):
            return
        encoded = line[5:].strip()
        if encoded == b"[DONE]":
            return
        try:
            value = json.loads(encoded.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if isinstance(value, dict) and not value.get("type") and event_name:
            value["type"] = event_name
        events.append(value)
        if isinstance(value, dict):
            event_type = str(value.get("type") or event_name)
            if event_type in {
                "response.output_text.delta",
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
                "response.refusal.delta",
                "response.function_call_arguments.delta",
                "response.custom_tool_call_input.delta",
            }:
                delta = value.get("delta")
                if isinstance(delta, str) and delta:
                    text_parts.append(delta)
                    if first_token_at is None:
                        first_token_at = time.monotonic()
            # The administrator Resin-username endpoint deliberately uses the
            # production Chat Completions protocol.  Track its choice deltas
            # with the same clock instead of requiring Responses-style event
            # names from the upstream node-level Quality Guard endpoint.
            choices = value.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        text_parts.append(content)
                        if first_token_at is None:
                            first_token_at = time.monotonic()

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            while True:
                if time.monotonic() - started > timeout:
                    raise TimeoutError("stream deadline exceeded")
                # read(n) may wait for n bytes or EOF. Typical SSE responses
                # are smaller than 64 KiB, which would collapse the entire
                # stream into one final chunk and destroy first-token timing.
                # Read one SSE line at a time so timestamps reflect arrival.
                line = response.readline(64 * 1024)
                if not line:
                    break
                raw_body.extend(line)
                if len(raw_body) > 64 * 1024 * 1024:
                    raise GuardError("stream response too large")
                consume_line(line)
    except urllib.error.HTTPError as exc:
        raw = exc.read(16 * 1024)
        raise APIError(exc.code, extract_error_code(raw)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GuardError(f"http request failed: {type(exc).__name__}") from None

    # A deployment can temporarily ignore stream=true and return one JSON
    # document.  Treat that as a valid fallback, but report streaming=false
    # so the speed signal is not mistaken for a panel-equivalent value.
    if not events and raw_body:
        try:
            value = json.loads(bytes(raw_body).decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            value = None
        if value is not None:
            events = [value]
            content_type = "application/json"

    finished = time.monotonic()
    return {
        "events": events,
        "text_parts": text_parts,
        "started": started,
        "finished": finished,
        "first_token_at": first_token_at,
        "streaming": "event-stream" in content_type or bool(text_parts),
    }


def extract_svg(value: Any) -> str:
    candidates: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            if "<svg" in item.lower() and "</svg>" in item.lower():
                candidates.append(item)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    if not candidates and isinstance(value, str):
        candidates = [value]
    for candidate in sorted(candidates, key=len, reverse=True):
        match = re.search(r"<svg\b[\s\S]*?</svg>", candidate, re.IGNORECASE)
        if match:
            svg = match.group(0).strip()
            svg = re.sub(r"^```(?:xml|svg|html)?\s*", "", svg, flags=re.IGNORECASE)
            return svg
    raise GuardError("svg extraction failed")


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def extract_usage(value: Any) -> tuple[int, int]:
    """Return output/reasoning tokens from JSON or a list of SSE events.

    Grok responses have appeared with both snake_case and camelCase usage
    fields.  The completed SSE event is nested under ``response`` while
    non-streaming responses put it at the top level, so recurse rather than
    depending on one exact envelope.
    """

    found: tuple[int, int] = (0, 0)

    def visit(item: Any) -> None:
        nonlocal found
        if isinstance(item, dict):
            usage = item.get("usage")
            if isinstance(usage, dict):
                output = _int_value(
                    usage.get("output_tokens")
                    or usage.get("outputTokens")
                    or usage.get("completion_tokens")
                    or usage.get("completionTokens")
                )
                details = (
                    usage.get("output_tokens_details")
                    or usage.get("outputTokensDetails")
                    or usage.get("completion_tokens_details")
                    or usage.get("completionTokensDetails")
                )
                reasoning = 0
                if isinstance(details, dict):
                    reasoning = _int_value(details.get("reasoning_tokens") or details.get("reasoningTokens"))
                if output or reasoning:
                    found = (output, reasoning)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def extract_response_id(value: Any) -> str:
    response_id = ""

    def visit(item: Any) -> None:
        nonlocal response_id
        if response_id:
            return
        if isinstance(item, dict):
            candidate = item.get("id")
            if isinstance(candidate, str) and (candidate.startswith("resp_") or candidate.startswith("response_")):
                response_id = candidate[:120]
                return
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return response_id


def stream_text_parts(events: list[Any]) -> list[str]:
    """Collect output-text deltas when a completed event omits full output."""

    parts: list[str] = []
    delta_types = {
        "response.output_text.delta",
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
        "response.refusal.delta",
        "response.function_call_arguments.delta",
        "response.custom_tool_call_input.delta",
    }
    done_types = {"response.output_text.done", "response.reasoning_summary_text.done"}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type in delta_types:
            value = event.get("delta")
            if isinstance(value, str):
                parts.append(value)
        elif event_type in done_types:
            value = event.get("text")
            if isinstance(value, str) and value:
                # A done event repeats the complete text after deltas.  It is
                # only a fallback; completed response output is preferred.
                parts.append(value)
    return parts


SVG_NS = "{http://www.w3.org/2000/svg}"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def safe_float(value: str) -> float:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value or "")
    return float(match.group(0)) if match else 0.0


def rendered_color_features(svg: str) -> list[float]:
    """Render SVG and return fixed-length color/edge features.

    The renderer is intentionally best-effort.  XML validity remains a hard
    gate, while a missing renderer contributes a fixed zero vector instead of
    changing the feature dimensionality or making the probe fail solely due
    to an optional image-analysis dependency.
    """
    width = height = 128
    feature_count = 11
    try:
        png = subprocess.run(
            ["rsvg-convert", "-w", str(width), "-h", str(height), "-"],
            input=svg.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )
        result = subprocess.run(
            ["convert", "png:-", "-depth", "8", "rgba:-"],
            input=png.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return [0.0] * feature_count
    raw = result.stdout
    if len(raw) != width * height * 4:
        return [0.0] * feature_count
    pixels = [raw[offset : offset + 4] for offset in range(0, len(raw), 4)]
    visible = [index for index, pixel in enumerate(pixels) if pixel[3] > 16]
    if not visible:
        return [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    rgb = [(pixels[index][0] / 255.0, pixels[index][1] / 255.0, pixels[index][2] / 255.0) for index in visible]
    luma = [0.2126 * red + 0.7152 * green + 0.0722 * blue for red, green, blue in rgb]
    saturation = []
    bins: list[tuple[int, int, int]] = []
    for red, green, blue in rgb:
        maximum = max(red, green, blue)
        minimum = min(red, green, blue)
        saturation.append(0.0 if maximum <= 0 else (maximum - minimum) / maximum)
        bins.append((int(red * 7), int(green * 7), int(blue * 7)))
    bin_counts: dict[tuple[int, int, int], int] = {}
    for item in bins:
        bin_counts[item] = bin_counts.get(item, 0) + 1

    luma_image = [0.2126 * (pixel[0] / 255.0) + 0.7152 * (pixel[1] / 255.0) + 0.0722 * (pixel[2] / 255.0) for pixel in pixels]
    mask = [pixel[3] > 16 for pixel in pixels]
    edges = comparisons = 0
    for y in range(height):
        for x in range(width):
            index = y * width + x
            if not mask[index]:
                continue
            for neighbor in ((index + 1) if x < width - 1 else -1, (index + width) if y < height - 1 else -1):
                if neighbor >= 0 and mask[neighbor]:
                    comparisons += 1
                    if abs(luma_image[index] - luma_image[neighbor]) > 0.12:
                        edges += 1

    mean_luma = sum(luma) / len(luma)
    mean_saturation = sum(saturation) / len(saturation)
    return [
        1.0,  # renderer_available
        sum(item[0] for item in rgb) / len(rgb),
        sum(item[1] for item in rgb) / len(rgb),
        sum(item[2] for item in rgb) / len(rgb),
        mean_luma,
        math.sqrt(sum((value - mean_luma) ** 2 for value in luma) / len(luma)),
        mean_saturation,
        math.sqrt(sum((value - mean_saturation) ** 2 for value in saturation) / len(saturation)),
        len(bin_counts) / 512.0,
        max(bin_counts.values()) / len(bins),
        edges / max(1, comparisons),
    ]


def svg_features(svg: str) -> tuple[list[float], ET.Element]:
    if len(svg.encode("utf-8")) > 32 * 1024 * 1024:
        raise GuardError("svg too large")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise GuardError("svg parse failed") from exc

    counts = {name: 0 for name in ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "g", "defs", "image", "use", "style", "lineargradient", "radialgradient", "foreignobject")}
    colors: set[str] = set()
    fills = strokes = 0
    path_chars = text_chars = attr_count = 0
    max_depth = 0
    max_stroke = 0.0
    for element in root.iter():
        name = local_name(element.tag)
        if name in counts:
            counts[name] += 1
        attr_count += len(element.attrib)
        max_depth = max(max_depth, 1 + element.tag.count("}"))
        for key, value in element.attrib.items():
            key = local_name(key)
            if key in {"fill", "stroke", "stop-color", "color", "background"}:
                normalized = value.strip().lower()
                if normalized and normalized not in {"none", "transparent", "inherit", "currentcolor"}:
                    colors.add(normalized)
            if key == "fill" and value.strip().lower() not in {"", "none"}:
                fills += 1
            if key == "stroke" and value.strip().lower() not in {"", "none"}:
                strokes += 1
            if key == "stroke-width":
                max_stroke = max(max_stroke, safe_float(value))
        path_chars += len(element.attrib.get("d", ""))
        text_chars += len("".join(element.itertext())) if name == "text" else 0

    viewbox = root.attrib.get("viewBox", "").replace(",", " ").split()
    vb_width = safe_float(viewbox[2]) if len(viewbox) >= 3 else safe_float(root.attrib.get("width", ""))
    vb_height = safe_float(viewbox[3]) if len(viewbox) >= 4 else safe_float(root.attrib.get("height", ""))
    ratio = vb_width / vb_height if vb_width > 0 and vb_height > 0 else 0.0
    style_text = " ".join(element.attrib.get("style", "") for element in root.iter()).lower()
    numeric_count = len(re.findall(r"[-+]?\d+(?:\.\d+)?", svg))
    vector = [
        1.0,  # XML is valid; invalid XML is handled as hard bad.
        math.log1p(len(svg)) / 10.0,
        math.log1p(sum(counts.values())),
        math.log1p(attr_count),
        math.log1p(max_depth),
        math.log1p(path_chars) / 4.0,
        math.log1p(text_chars) / 4.0,
        math.log1p(numeric_count),
        math.log1p(len(colors)),
        math.log1p(fills), math.log1p(strokes), max_stroke / 10.0,
        ratio / 4.0,
        math.log1p(vb_width) / 4.0, math.log1p(vb_height) / 4.0,
    ]
    vector.extend(math.log1p(counts[name]) for name in counts)
    vector.extend([
        1.0 if "gradient" in style_text else 0.0,
        1.0 if "clip-path" in svg else 0.0,
        1.0 if "filter" in svg else 0.0,
        1.0 if "animate" in svg else 0.0,
    ])
    vector.extend(rendered_color_features(svg))
    return vector, root


@dataclass
class KNNClassifier:
    vectors: list[list[float]]
    labels: list[str]
    means: list[float]
    scales: list[float]

    @classmethod
    def from_snapshot(cls, path: Path) -> "KNNClassifier":
        value = read_json(path, {})
        if not isinstance(value, dict) or not value.get("vectors"):
            raise GuardError("fixed classifier snapshot is invalid")
        return cls(value["vectors"], value["labels"], value["means"], value["scales"])

    @classmethod
    def from_annotations(cls) -> "KNNClassifier":
        annotations = read_json(ANNOTATIONS_PATH, {})
        vectors: list[list[float]] = []
        labels: list[str] = []
        if isinstance(annotations, dict):
            for relative, label in annotations.items():
                if label not in {"good", "bad", "uncertain"}:
                    continue
                if relative.startswith("samples/"):
                    relative = relative[len("samples/"):]
                path = ROOT / relative if relative.startswith("runtime-samples/") else ROOT / "samples" / relative
                try:
                    vector, _ = svg_features(path.read_text(encoding="utf-8", errors="replace"))
                except GuardError:
                    continue
                vectors.append(vector)
                labels.append(label)
        if not vectors:
            return cls([], [], [], [])
        width = len(vectors[0])
        means = [sum(row[i] for row in vectors) / len(vectors) for i in range(width)]
        scales = []
        for i in range(width):
            variance = sum((row[i] - means[i]) ** 2 for row in vectors) / len(vectors)
            scales.append(math.sqrt(variance) or 1.0)
        return cls(vectors, labels, means, scales)

    def classify(self, svg: str) -> tuple[str, float, dict[str, Any]]:
        try:
            vector, _ = svg_features(svg)
        except GuardError as exc:
            return "bad", 1.0, {"reason": str(exc)}
        if not self.vectors:
            return "uncertain", 0.0, {"reason": "no_labeled_samples"}
        normalized = [(vector[i] - self.means[i]) / self.scales[i] for i in range(len(vector))]
        distances: list[tuple[float, str]] = []
        for sample, label in zip(self.vectors, self.labels):
            sample_norm = [(sample[i] - self.means[i]) / self.scales[i] for i in range(len(vector))]
            distance = math.sqrt(sum((normalized[i] - sample_norm[i]) ** 2 for i in range(len(vector))))
            distances.append((distance, label))
        distances.sort(key=lambda item: item[0])
        neighbors = distances[: min(5, len(distances))]
        weights: dict[str, float] = {"good": 0.0, "bad": 0.0, "uncertain": 0.0}
        for distance, label in neighbors:
            weights[label] += 1.0 / (distance + 0.15)
        total = sum(weights.values()) or 1.0
        for key in weights:
            weights[key] /= total
        label = max(weights, key=weights.get)
        nearest = neighbors[0][0] if neighbors else 999.0
        # Samples outside the annotated manifold are deliberately neutral.
        if nearest > 8.0 or weights[label] < 0.42:
            label = "uncertain"
        return label, float(weights.get(label, 0.0)), {"nearest": round(nearest, 4), "weights": {k: round(v, 4) for k, v in weights.items()}, "sample_count": len(self.vectors)}


class GrokClient:
    def __init__(self) -> None:
        self.admin_token = ""
        self.client_secret = ""
        self.client_key_id = ""
        self.client_key_name = ""

    def login(self) -> None:
        username, password = read_bootstrap_admin(GROK_CONFIG_PATH)
        result = unwrap_data(http_json("POST", "/api/admin/v1/auth/login", {"username": username, "password": password}, timeout=20))
        token = result.get("tokens", {}).get("accessToken", "") if isinstance(result, dict) else ""
        if not token:
            raise GuardError("admin login returned no access token")
        self.admin_token = token
        self.client_secret = ""
        self.discover_client_key()
        safe_log("grok_admin_authenticated", client_key_name=self.client_key_name, client_key_id=self.client_key_id)

    def admin_request(self, method: str, path: str, body: Any | None = None, timeout: float = 30.0) -> Any:
        if not self.admin_token:
            self.login()
        try:
            return unwrap_data(http_json(method, path, body, token=self.admin_token, timeout=timeout))
        except APIError as exc:
            if exc.status != 401:
                raise
            self.login()
            return unwrap_data(http_json(method, path, body, token=self.admin_token, timeout=timeout))

    def discover_client_key(self) -> None:
        data = self.admin_request("GET", "/api/admin/v1/client-keys?page=1&pageSize=100")
        items = data.get("items", []) if isinstance(data, dict) else []
        candidates = [item for item in items if item.get("enabled")]
        if CLIENT_KEY_NAME:
            candidates = [item for item in candidates if item.get("name") == CLIENT_KEY_NAME]
        elif CLIENT_KEY_PREFIX:
            candidates = [item for item in candidates if item.get("prefix") == CLIENT_KEY_PREFIX]
        if not candidates:
            raise GuardError("no enabled client key matches guard selection")

        def score(item: dict[str, Any]) -> tuple[int, str]:
            name = str(item.get("name", "")).lower()
            preferred = int(any(word in name for word in ("guard", "test", "probe")))
            return preferred, str(item.get("lastUsedAt") or "")

        selected = sorted(candidates, key=score, reverse=True)[0]
        key_id = str(selected.get("id", ""))
        secret_data = self.admin_request("GET", f"/api/admin/v1/client-keys/{urllib.parse.quote(key_id, safe='')}/secret")
        secret = secret_data.get("secret", "") if isinstance(secret_data, dict) else ""
        if not secret:
            raise GuardError("selected client key has no readable secret")
        self.client_key_id = key_id
        self.client_key_name = str(selected.get("name", ""))
        self.client_secret = secret

    def list_build_nodes(self) -> list[dict[str, Any]]:
        data = self.admin_request("GET", "/api/admin/v1/egress-nodes?scope=grok_build")
        return data.get("items", []) if isinstance(data, dict) else []

    def select_node(self) -> dict[str, Any]:
        nodes = self.list_build_nodes()
        if NODE_ID:
            for node in nodes:
                if str(node.get("id")) == NODE_ID:
                    return node
        for node in nodes:
            if str(node.get("name", "")) == NODE_NAME:
                return node
        raise GuardError("Build egress node not found")

    def update_build_proxy(self, node: dict[str, Any], proxy_url: str) -> dict[str, Any]:
        body = {
            "name": node.get("name", NODE_NAME),
            "scope": node.get("scope", "grok_build"),
            "enabled": bool(node.get("enabled", True)),
            "proxyPool": bool(node.get("proxyPool", True)),
            "accountCapacity": int(node.get("accountCapacity", 0) or 0),
            "userAgent": node.get("userAgent", ""),
            "proxyURL": proxy_url,
        }
        if DRY_RUN:
            safe_log("dry_run_proxy_update", node_id=str(node.get("id")))
            return node
        data = self.admin_request("PUT", f"/api/admin/v1/egress-nodes/{urllib.parse.quote(str(node['id']), safe='')}", body, timeout=30)
        return data if isinstance(data, dict) else node

    def ensure_build_account_proxy(self, node_id: int) -> dict[str, Any]:
        """Keep the Build Resin node account-bound instead of pinning one lease.

        The Pelican selector supplies a Resin username at request time.  That
        identity only reaches Resin when the saved proxy URL contains
        ``{account}``; an old external guard used to save a concrete username.
        Reconcile the persisted node on every guard start so an operator edit
        or legacy deployment cannot silently bypass the selector.
        """
        for node in self.list_build_nodes():
            if str(node.get("id")) != str(node_id):
                continue
            if bool(node.get("accountBoundProxy")):
                return node
            # ``build_proxy_url`` percent-encodes braces as it should for a
            # concrete URL.  The egress normalizer, however, must receive the
            # literal marker in order to turn it into its safe sentinel before
            # parsing and persist it as an account template.
            template = build_proxy_url("{account}").replace("%7Baccount%7D", "{account}")
            updated = self.update_build_proxy(node, template)
            safe_log("build_resin_account_template_restored", node_id=str(node_id))
            return updated
        raise GuardError(f"Build egress node {node_id} not found")

    def generate(self, prompt: str, timeout: float, stream: bool = True) -> GenerationResult:
        if not self.client_secret:
            self.login()
        body = {"model": MODEL, "input": prompt, "stream": bool(stream)}
        if stream:
            try:
                envelope = http_sse("POST", "/v1/responses", body, token=self.client_secret, timeout=timeout)
            except APIError as exc:
                if exc.status != 401:
                    raise
                self.login()
                envelope = http_sse("POST", "/v1/responses", body, token=self.client_secret, timeout=timeout)
            events = envelope.get("events", [])
            try:
                svg = extract_svg(events)
            except GuardError:
                parts = envelope.get("text_parts", [])
                svg = extract_svg("".join(parts))
            started = float(envelope.get("started") or time.monotonic())
            finished = float(envelope.get("finished") or time.monotonic())
            first_token_at = envelope.get("first_token_at")
            duration_ms = max(0, int((finished - started) * 1000))
            first_token_ms = None
            if isinstance(first_token_at, (int, float)) and first_token_at >= started:
                first_token_ms = max(0, int((first_token_at - started) * 1000))
            output_tokens, reasoning_tokens = extract_usage(events)
            generation_ms = duration_ms - (first_token_ms or 0)
            speed = None
            if first_token_ms is not None and generation_ms > 0 and output_tokens > 0:
                speed = output_tokens * 1000.0 / generation_ms
            return GenerationResult(
                svg=svg,
                duration_ms=duration_ms,
                first_token_ms=first_token_ms,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                output_tokens_per_second=speed,
                response_id=extract_response_id(events),
                streaming=bool(envelope.get("streaming")),
            )

        started = time.monotonic()
        try:
            result = http_json("POST", "/v1/responses", body, token=self.client_secret, timeout=timeout)
        except APIError as exc:
            if exc.status != 401:
                raise
            self.login()
            started = time.monotonic()
            result = http_json("POST", "/v1/responses", body, token=self.client_secret, timeout=timeout)
        finished = time.monotonic()
        output_tokens, reasoning_tokens = extract_usage(result)
        return GenerationResult(
            svg=extract_svg(result),
            duration_ms=max(0, int((finished - started) * 1000)),
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            response_id=extract_response_id(result),
            streaming=False,
        )


def build_proxy_url(account: str) -> str:
    resin_env = read_env_file(RESIN_ENV_PATH)
    token = os.environ.get("GUARD_RESIN_PROXY_TOKEN", "") or resin_env.get("RESIN_PROXY_TOKEN", "")
    if not token:
        raise GuardError("Resin proxy token is unavailable")
    scheme = os.environ.get("GUARD_PROXY_SCHEME", "socks5h")
    host = os.environ.get("GUARD_PROXY_HOST", "resin")
    port = os.environ.get("GUARD_PROXY_PORT", "2260")
    platform = os.environ.get("GUARD_RESIN_PLATFORM", "Default")
    account = str(account).strip()
    # Pool entries may already contain the Resin platform prefix (for
    # example ``Default.pelican-...``).  Do not turn that into the invalid
    # ``Default.Default.pelican-...`` username.
    username = account if account.startswith(platform + ".") else f"{platform}.{account}"
    return f"{scheme}://{urllib.parse.quote(username, safe='.') }:{urllib.parse.quote(token, safe='')}@{host}:{port}"


class Guard:
    def __init__(self) -> None:
        policy = read_json(POLICY_PATH, {})
        self.prompt = str(policy.get("prompt") or "画一个鹈鹕骑自行车的svg")
        self.interval = int(policy.get("probe_interval_seconds", 600))
        self.timeout = int(policy.get("request_timeout_seconds", 180))
        self.probe_stream = bool(policy.get("probe_stream", True))
        rotation = policy.get("rotation", {})
        self.max_rotation_seconds = max(1, int(rotation.get("max_duration_seconds", 540)))
        self.delay = max(0, int(rotation.get("delay_seconds", 3)))
        self.uncertain_rotate_after = max(1, int(rotation.get("uncertain_rotate_after_consecutive", 3)))
        token_quality = policy.get("token_quality", {})
        self.token_quality_enabled = bool(token_quality.get("enabled", False))
        self.token_speed_soft = max(0.0, float(token_quality.get("soft_tps", 250.0)))
        self.token_speed_hard = max(self.token_speed_soft, float(token_quality.get("hard_tps", 500.0)))
        self.token_speed_min_tokens = max(1, int(token_quality.get("min_output_tokens", 32)))
        self.classifier = KNNClassifier.from_annotations()
        try:
            self.annotations_mtime_ns = ANNOTATIONS_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            self.annotations_mtime_ns = 0
        self.client = GrokClient()
        self.state = read_json(STATE_PATH, {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.node: dict[str, Any] | None = None
        self.account_prefix = os.environ.get("GUARD_ACCOUNT_PREFIX", "grok-build-guard").strip() or "grok-build-guard"

    def save_state(self) -> None:
        write_json_atomic(STATE_PATH, self.state)

    def new_account(self) -> str:
        return f"{self.account_prefix}-{int(time.time())}-{secrets.token_hex(4)}"

    def ensure_node(self) -> dict[str, Any]:
        self.node = self.client.select_node()
        return self.node

    def switch_account(self, account: str) -> None:
        node = self.ensure_node()
        self.client.update_build_proxy(node, build_proxy_url(account))
        self.state["current_account"] = account
        self.state["updated_at"] = now_iso()
        self.save_state()
        safe_log("build_proxy_account_selected", node_id=str(node.get("id")), account_hash=redact_hash(account))

    def token_quality_label(self, label: str, generation: GenerationResult, details: dict[str, Any]) -> str:
        speed = generation.output_tokens_per_second
        details.update({
            "duration_ms": generation.duration_ms,
            "first_token_ms": generation.first_token_ms,
            "output_tokens": generation.output_tokens,
            "reasoning_tokens": generation.reasoning_tokens,
            "output_tokens_per_second": None if speed is None else round(speed, 4),
            "streaming": generation.streaming,
        })
        if speed is None:
            details["token_speed_band"] = "unavailable"
        elif speed < self.token_speed_soft:
            details["token_speed_band"] = "low"
        elif speed <= self.token_speed_hard:
            details["token_speed_band"] = "mid"
        else:
            details["token_speed_band"] = "high"
        if not self.token_quality_enabled or speed is None or generation.output_tokens < self.token_speed_min_tokens:
            return label
        if speed < self.token_speed_soft:
            details["token_quality"] = "low_speed_good"
            return "good"
        if speed <= self.token_speed_hard:
            details["token_quality"] = "mid_speed_uncertain"
            return "uncertain"
        details["token_quality"] = "high_speed_bad"
        return "bad"

    def probe(self, account: str, timeout_override: float | None = None) -> tuple[str, dict[str, Any]]:
        started = time.monotonic()
        svg = ""
        generation: GenerationResult | None = None
        try:
            # The annotator writes annotations.json atomically.  Pick up new
            # human labels at the next probe without restarting the service.
            try:
                annotations_mtime = ANNOTATIONS_PATH.stat().st_mtime_ns
            except FileNotFoundError:
                annotations_mtime = 0
            if annotations_mtime != self.annotations_mtime_ns:
                self.classifier = KNNClassifier.from_annotations()
                self.annotations_mtime_ns = annotations_mtime
                safe_log("classifier_reloaded", classifier_samples=len(self.classifier.vectors))
            if DRY_RUN:
                # Exercise the local classifier without making a model call.
                fixture = ROOT / "samples" / "positive" / "diagram.svg"
                svg = fixture.read_text(encoding="utf-8")
            else:
                request_timeout = self.timeout if timeout_override is None else max(1.0, timeout_override)
                generation = self.client.generate(self.prompt, request_timeout, stream=self.probe_stream)
                svg = generation.svg
            label, confidence, details = self.classifier.classify(svg)
            if generation is not None:
                label = self.token_quality_label(label, generation, details)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            safe_log("probe_completed", label=label, confidence=round(confidence, 4), elapsed_ms=elapsed_ms, **details)
            self.persist_sample(svg, label, account, generation)
            event: dict[str, Any] = {"event": "probe", "label": label, "confidence": round(confidence, 4), "account_hash": redact_hash(account), "elapsed_ms": elapsed_ms}
            if generation is not None:
                event.update({
                    "duration_ms": generation.duration_ms,
                    "first_token_ms": generation.first_token_ms,
                    "output_tokens": generation.output_tokens,
                    "reasoning_tokens": generation.reasoning_tokens,
                    "output_tokens_per_second": generation.output_tokens_per_second,
                    "token_speed_band": details.get("token_speed_band"),
                    "streaming": generation.streaming,
                })
            append_event(event)
            return label, details
        except (GuardError, APIError) as exc:
            label = "bad"
            reason = str(exc)
            # APIError's string contains only status/code; no response body.
            safe_log("probe_failed", reason=reason, elapsed_ms=int((time.monotonic() - started) * 1000))
            append_event({"event": "probe", "label": label, "reason": reason, "account_hash": redact_hash(account), "elapsed_ms": int((time.monotonic() - started) * 1000)})
            return label, {"reason": reason}

    def persist_sample(self, svg: str, label: str, account: str, generation: GenerationResult | None = None) -> None:
        if MAX_SAVED_SAMPLES == 0:
            return
        SAMPLES_PATH.mkdir(parents=True, exist_ok=True)
        # Count only completed SVG files.  JSON sidecars are written together
        # with their SVG, so a partial/interrupted write cannot consume the
        # quota.  The cap is deliberately independent from classification:
        # once full, probing and rotation continue normally without disk
        # growth.
        saved = sum(1 for _ in SAMPLES_PATH.glob("*.svg"))
        if saved >= MAX_SAVED_SAMPLES:
            if not self.state.get("sample_storage_full"):
                self.state["sample_storage_full"] = True
                self.save_state()
                safe_log("sample_storage_full", max_saved_samples=MAX_SAVED_SAMPLES)
                append_event({"event": "sample_storage_full", "max_saved_samples": MAX_SAVED_SAMPLES})
            return
        stem = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + redact_hash(account)
        path = SAMPLES_PATH / f"{stem}.svg"
        path.write_text(svg, encoding="utf-8")
        meta: dict[str, Any] = {
            "svg": path.name,
            "label": label,
            "account_hash": redact_hash(account),
            "prompt": self.prompt,
            "model": MODEL,
            "svg_bytes": len(svg.encode("utf-8")),
        }
        if generation is not None:
            meta.update({
                "streaming": generation.streaming,
                "duration_ms": generation.duration_ms,
                "first_token_ms": generation.first_token_ms,
                "output_tokens": generation.output_tokens,
                "reasoning_tokens": generation.reasoning_tokens,
                "output_tokens_per_second": generation.output_tokens_per_second,
                "token_speed_band": (
                    "unavailable" if generation.output_tokens_per_second is None
                    else "low" if generation.output_tokens_per_second < self.token_speed_soft
                    else "mid" if generation.output_tokens_per_second <= self.token_speed_hard
                    else "high"
                ),
            })
        (SAMPLES_PATH / f"{stem}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def rotate(self) -> str:
        resume = self.state.get("state") == "rotating" and bool(self.state.get("rotation_started_at"))
        self.state["state"] = "rotating"
        if not resume:
            self.state["rotation_started_at"] = now_iso()
            self.state["attempts"] = 0
        self.state["uncertain_streak"] = 0
        self.save_state()
        try:
            started_at = dt.datetime.fromisoformat(str(self.state["rotation_started_at"])).timestamp()
        except (KeyError, TypeError, ValueError, OverflowError):
            started_at = time.time()
            self.state["rotation_started_at"] = dt.datetime.fromtimestamp(started_at, dt.timezone.utc).isoformat()
        attempt = int(self.state.get("attempts", 0) or 0)
        while True:
            elapsed = time.time() - started_at
            remaining = self.max_rotation_seconds - elapsed
            if remaining <= 0:
                break
            if attempt > 0 and self.delay:
                if remaining <= self.delay:
                    break
                time.sleep(self.delay)
                remaining = self.max_rotation_seconds - (time.time() - started_at)
                if remaining <= 0:
                    break
            attempt += 1
            account = self.new_account()
            self.state["attempts"] = attempt
            self.save_state()
            try:
                self.switch_account(account)
            except (GuardError, APIError) as exc:
                safe_log("rotation_account_switch_failed", attempt=attempt, reason=str(exc))
                append_event({"event": "rotation_attempt", "attempt": attempt, "label": "bad", "reason": str(exc)})
                continue
            remaining = self.max_rotation_seconds - (time.time() - started_at)
            if remaining <= 0:
                break
            label, _ = self.probe(account, timeout_override=min(float(self.timeout), remaining))
            append_event({"event": "rotation_attempt", "attempt": attempt, "label": label, "account_hash": redact_hash(account)})
            if label == "good":
                self.state.update({"state": "healthy", "current_account": account, "last_label": label, "last_probe_at": now_iso(), "attempts": attempt, "uncertain_streak": 0, "last_good_at": now_iso(), "rotation_elapsed_seconds": round(time.time() - started_at, 1)})
                self.save_state()
                safe_log("rotation_succeeded", attempts=attempt, elapsed_seconds=round(time.time() - started_at, 1))
                return "good"
        elapsed = max(0.0, time.time() - started_at)
        self.state.update({"state": "exhausted", "last_label": "bad", "last_probe_at": now_iso(), "rotation_elapsed_seconds": round(elapsed, 1)})
        self.save_state()
        safe_log("rotation_exhausted", attempts=attempt, elapsed_seconds=round(elapsed, 1), budget_seconds=self.max_rotation_seconds)
        return "bad"

    def cycle(self) -> str:
        account = str(self.state.get("current_account", ""))
        if not account:
            account = self.new_account()
            self.switch_account(account)
        current_state = str(self.state.get("state", "unknown"))
        if current_state == "rotating":
            # If the process restarted during a rotation, resume the policy
            # rather than treating the in-flight candidate as a normal
            # healthy/unknown probe.
            return self.rotate()
        if current_state == "exhausted":
            return self.rotate()
        label, _ = self.probe(account)
        self.state.update({"last_label": label, "last_probe_at": now_iso()})
        if label == "good":
            self.state["state"] = "healthy"
            self.state["uncertain_streak"] = 0
            self.state["last_good_at"] = now_iso()
            self.save_state()
            return label
        if label == "uncertain":
            streak = int(self.state.get("uncertain_streak", 0) or 0) + 1
            self.state["uncertain_streak"] = streak
            if streak >= self.uncertain_rotate_after:
                self.save_state()
                safe_log("uncertain_streak_triggered_rotation", streak=streak, threshold=self.uncertain_rotate_after)
                return self.rotate()
            # Unknown/healthy both keep the current account until the
            # configured consecutive-uncertain threshold. Rotating never
            # calls cycle() until it has explicitly obtained good.
            self.state["state"] = "healthy" if current_state == "healthy" else "unknown"
            self.save_state()
            return label
        self.state["uncertain_streak"] = 0
        self.save_state()
        return self.rotate()

    def run(self) -> None:
        safe_log("guard_starting", model=MODEL, interval_seconds=self.interval, timeout_seconds=self.timeout, probe_stream=self.probe_stream, token_quality_enabled=self.token_quality_enabled, token_speed_soft=self.token_speed_soft, token_speed_hard=self.token_speed_hard, rotation_budget_seconds=self.max_rotation_seconds, delay_seconds=self.delay, uncertain_rotate_after=self.uncertain_rotate_after, dry_run=DRY_RUN, classifier_samples=len(self.classifier.vectors), max_saved_samples=MAX_SAVED_SAMPLES)
        # Login and node discovery are explicit startup checks.  The first
        # request also refreshes auth automatically if an access token expires.
        self.client.login()
        self.ensure_node()
        # A state written by an older/dry-run process may say healthy without
        # ever recording a real good SVG.  Treat that as unknown so a
        # simulated result can never suppress the first production decision.
        if self.state.get("state") == "healthy" and not self.state.get("last_good_at"):
            self.state["state"] = "unknown"
            self.save_state()
        # A persisted account is only a logical identity until the egress
        # node has been updated after a restart.  Re-apply it once so a
        # restart cannot silently probe the previous proxy identity.
        persisted_account = str(self.state.get("current_account", ""))
        if persisted_account and str(self.state.get("state", "")) != "exhausted":
            self.switch_account(persisted_account)
        if ONESHOT:
            self.cycle()
            return
        if self.state.get("state") == "exhausted":
            try:
                last_probe = dt.datetime.fromisoformat(str(self.state.get("last_probe_at"))).timestamp()
                cooldown = max(0.0, self.interval - (time.time() - last_probe))
            except (TypeError, ValueError, OverflowError):
                cooldown = 0.0
            if cooldown > 0:
                safe_log("exhausted_waiting_next_cycle", wait_seconds=round(cooldown, 1))
                time.sleep(cooldown)
        while True:
            try:
                self.cycle()
            except Exception as exc:  # keep the watchdog alive; retry next cycle
                detail: dict[str, Any] = {}
                if isinstance(exc, FileNotFoundError):
                    # FileNotFoundError is intentionally logged only with a
                    # basename; this helps diagnose disappearing sample
                    # files without exposing mounted secret paths.
                    detail["filename"] = Path(exc.filename).name if exc.filename else ""
                safe_log("guard_cycle_failed", reason=type(exc).__name__, **detail)
                append_event({"event": "cycle_failed", "reason": type(exc).__name__, **detail})
            time.sleep(max(5, self.interval))


def main() -> int:
    try:
        Guard().run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        safe_log("guard_start_failed", reason=type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
