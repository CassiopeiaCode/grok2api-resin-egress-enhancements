#!/usr/bin/env python3
"""Maintains the fixed-size Build Resin good-username pool."""
from __future__ import annotations
import datetime as dt
import os
import random
import re
import secrets
import subprocess
import time
from guard import APIError, GrokClient, build_proxy_url, extract_usage, http_sse, safe_log, MODEL

PROMPT = "Write exactly 16 numbered lines about reliable distributed systems. Each line must be one complete English sentence, with no markdown heading. The final line must end with the exact marker QUALITY_OK."
EXPECTED = "QUALITY_OK"
TPS_THRESHOLD = 200.0
MAX_OUTPUT_TOKENS = 384
POOL_TARGET = int(os.environ.get("PELICAN_POOL_TARGET", "5"))
NODE_ID = int(os.environ.get("PELICAN_NODE_ID", "33"))

def accounts(client):
    # v3.1.1's status=active list filter also applies routing/quota state and
    # can legitimately return zero even when many enabled credentials are
    # suitable for an administrator probe. Fetch the provider population and
    # apply the explicit probe-safe checks below instead.
    data = client.admin_request("GET", "/api/admin/v1/accounts?page=1&pageSize=500&provider=grok_build")
    strict=[]
    fallback=[]
    for x in (data.get("items", []) if isinstance(data, dict) else []):
        quota=x.get("quota") or {}
        cooldown=x.get("cooldownUntil", x.get("cooldown_until"))
        if not x.get("enabled", True): continue
        if str(x.get("authStatus", x.get("auth_status", "active"))).lower() not in {"active", ""}: continue
        fallback.append(x)
        cooling=False
        if cooldown:
            try: cooling=dt.datetime.fromisoformat(str(cooldown).replace("Z","+00:00")).timestamp() > time.time()
            except (TypeError,ValueError,OverflowError): cooling=True
        if int(x.get("failureCount", x.get("failure_count", 0)) or 0) > 0 or cooling: continue
        if quota.get("remaining") is not None and float(quota.get("remaining") or 0) <= 0: continue
        strict.append(x)
    # Administrator quality requests intentionally bypass production quota
    # leases and cooldown. Prefer a clean account when one exists, but do not
    # leave the Resin pool permanently empty when every account has stale
    # waitingReset/remaining=0 metadata. The real upstream request remains the
    # authority: unusable fallback accounts simply produce a failed probe.
    return strict or fallback

def username():
    return "Default.pelican-" + secrets.token_hex(16)

def trace_ip(proxy_username: str) -> str:
    """Resolve the public IP actually used by this Resin username.

    The blacklist is intentionally based on this value rather than the
    Resin username: multiple usernames can be assigned to the same egress
    and must share the same 24-hour quarantine.
    """
    try:
        result = subprocess.run(
            [
                "curl", "-fsSL", "--connect-timeout", "10", "--max-time", "20",
                "--proxy", build_proxy_url(proxy_username),
                "https://cloudflare.com/cdn-cgi/trace",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=25,
            check=True,
            text=True,
        )
        match = re.search(r"(?m)^ip=([^\r\n]+)", result.stdout)
        ip = match.group(1).strip() if match else ""
        if not ip:
            safe_log("pelican_trace_missing_ip", username_hash=proxy_username[-12:])
        return ip
    except subprocess.CalledProcessError as exc:
        safe_log("pelican_trace_failed", username_hash=proxy_username[-12:], exit_code=int(exc.returncode))
        return ""
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        safe_log("pelican_trace_failed", username_hash=proxy_username[-12:], error=type(exc).__name__)
        return ""

def due(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() <= time.time()
    except (TypeError, ValueError, OverflowError):
        return True

def probe(client, account_id, proxy_username):
    body = {"provider":"grok_build", "account_id":int(account_id), "egress_node_id":NODE_ID, "proxy_username":proxy_username,
            "request":{"model":MODEL, "stream":True, "stream_options":{"include_usage":True},
                       "max_tokens":MAX_OUTPUT_TOKENS,
                       "messages":[{"role":"user", "content":PROMPT}]}}
    started=time.monotonic()
    try:
        envelope=http_sse("POST", "/api/admin/v1/quality-tests/requests", body, token=client.admin_token, timeout=190)
        events=envelope.get("events", [])
        parts=list(envelope.get("text_parts", []))
        if not parts:
            for event in events:
                if not isinstance(event, dict): continue
                for choice in event.get("choices", []) if isinstance(event.get("choices"), list) else []:
                    delta=choice.get("delta", {}) if isinstance(choice,dict) else {}
                    if isinstance(delta,dict) and isinstance(delta.get("content"),str): parts.append(delta["content"])
        text="".join(parts)
        output_tokens, reasoning_tokens=extract_usage(events)
        stream_started=float(envelope.get("started") or started)
        stream_finished=float(envelope.get("finished") or time.monotonic())
        first_token_at=envelope.get("first_token_at")
        first_token_ms=None
        if isinstance(first_token_at, (int,float)) and first_token_at >= stream_started:
            first_token_ms=max(0,int((first_token_at-stream_started)*1000))
        duration_ms=max(0,int((stream_finished-stream_started)*1000))
        generation_ms=duration_ms-(first_token_ms or 0)
        speed=None
        if first_token_ms is not None and generation_ms > 0 and output_tokens > 0:
            speed=float(output_tokens)*1000.0/float(generation_ms)
        if EXPECTED not in text:
            label="expected_marker_missing"
        elif output_tokens < 32:
            label="insufficient_output_tokens"
        elif speed is None:
            label="missing_generation_window"
        elif speed > TPS_THRESHOLD:
            label="fast_tps"
        else:
            label="good"
        good=label=="good"
        confidence=1.0
        details={"output_tokens":output_tokens,"reasoning_tokens":reasoning_tokens,"first_token_ms":first_token_ms,
                 "duration_ms":duration_ms,"generation_ms":generation_ms,"output_tokens_per_second":speed,
                 "expected_matched":EXPECTED in text}
        safe_log("health_pool_probe", account_id=int(account_id), label=label, good=good,
                 output_tps=None if speed is None else round(speed,4), output_tokens=output_tokens,
                 first_token_ms=first_token_ms, duration_ms=duration_ms,
                 elapsed_ms=int((time.monotonic()-started)*1000), username_hash=proxy_username[-12:])
        return good, label, confidence, details
    except APIError as exc:
        safe_log("pelican_probe_failed", account_id=int(account_id), elapsed_ms=int((time.monotonic()-started)*1000), error=type(exc).__name__, api_status=exc.status, api_code=exc.code)
        return False, str(exc.code or "failed"), 0.0, {}
    except Exception as exc:
        safe_log("pelican_probe_failed", account_id=int(account_id), elapsed_ms=int((time.monotonic()-started)*1000), error=type(exc).__name__)
        return False, "failed", 0.0, {}

def main():
    client=GrokClient(); client.login(); client.ensure_build_account_proxy(NODE_ID)
    while True:
        try:
            pool=client.admin_request("GET", "/api/admin/v1/pelican-egress-pool").get("items", [])
            bad_ips=set(client.admin_request("GET", "/api/admin/v1/pelican-egress-pool/bad").get("items", []))
            # An admitted lease is not actively re-probed. Production Build
            # streams are the ongoing quality signal; Grok2API evicts a lease
            # after two consecutive measured streams above 200 tok/s. Once
            # evicted, this loop observes the deficit and explores a replacement.
            if len(pool) < POOL_TARGET:
                choices=accounts(client)
                if choices:
                    aid=random.choice(choices).get("id")
                    for attempt in range(1,11):
                        name=username()
                        exit_ip=trace_ip(name)
                        if exit_ip and exit_ip in bad_ips and attempt < 10:
                            safe_log("pelican_bad_ip_skipped", attempt=attempt, exit_ip=exit_ip, username_hash=name[-12:])
                            continue
                        good,label,conf,_=probe(client,aid,name)
                        if good and not exit_ip:
                            # A transient trace failure before generation
                            # must not admit an IP-less good entry. Retry the
                            # trace after the model request has completed.
                            exit_ip=trace_ip(name)
                        if good and not exit_ip:
                            safe_log("pelican_good_without_exit_ip", username_hash=name[-12:])
                            continue
                        client.admin_request("POST", "/api/admin/v1/pelican-egress-pool/results", {"proxy_username":name,"exit_ip":exit_ip,"label":label,"confidence":conf,"classifier_version":"grok2api-quality-guard-tps-200-v1"})
                        break
            time.sleep(3 if len(pool) < POOL_TARGET else 30)
        except Exception as exc:
            safe_log("pelican_pool_cycle_failed", error=type(exc).__name__); time.sleep(30)

if __name__ == "__main__": main()
