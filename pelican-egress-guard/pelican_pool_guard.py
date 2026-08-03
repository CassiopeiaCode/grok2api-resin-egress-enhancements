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
from pathlib import Path
from guard import APIError, GrokClient, KNNClassifier, build_proxy_url, extract_svg, http_sse, safe_log, MODEL

PROMPT = "画一个鹈鹕骑自行车的svg"
THRESHOLD = 0.55
POOL_TARGET = int(os.environ.get("PELICAN_POOL_TARGET", "5"))
NODE_ID = int(os.environ.get("PELICAN_NODE_ID", "33"))

def accounts(client):
    data = client.admin_request("GET", "/api/admin/v1/accounts?page=1&pageSize=500&provider=grok_build&status=active")
    out=[]
    for x in (data.get("items", []) if isinstance(data, dict) else []):
        quota=x.get("quota") or {}
        cooldown=x.get("cooldownUntil", x.get("cooldown_until"))
        if not x.get("enabled", True): continue
        if str(x.get("authStatus", x.get("auth_status", "active"))).lower() not in {"active", ""}: continue
        cooling=False
        if cooldown:
            try: cooling=dt.datetime.fromisoformat(str(cooldown).replace("Z","+00:00")).timestamp() > time.time()
            except (TypeError,ValueError,OverflowError): cooling=True
        if int(x.get("failureCount", x.get("failure_count", 0)) or 0) > 0 or cooling: continue
        if quota.get("remaining") is not None and float(quota.get("remaining") or 0) <= 0: continue
        out.append(x)
    return out

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

def probe(client, classifier, account_id, proxy_username):
    # Keep the actual Pelican prompt as the final user turn, but prepend one
    # harmless conversation turn.  This avoids reusing an identical one-turn
    # conversation while preserving the exact production probe prompt.
    nonce = secrets.randbelow(1_000_000_000)
    body = {"provider":"grok_build", "account_id":int(account_id), "egress_node_id":NODE_ID, "proxy_username":proxy_username,
            "request":{"model":MODEL, "stream":True, "messages":[
                {"role":"user", "content":f"请忽略：{nonce}"},
                {"role":"assistant", "content":"ok"},
                {"role":"user", "content":PROMPT},
            ]}}
    started=time.monotonic()
    try:
        envelope=http_sse("POST", "/api/admin/v1/quality-tests/requests", body, token=client.admin_token, timeout=190)
        parts=[]
        for event in envelope.get("events", []):
            if not isinstance(event, dict): continue
            value=event.get("delta")
            if isinstance(value,str): parts.append(value)
            for choice in event.get("choices", []) if isinstance(event.get("choices"), list) else []:
                delta=choice.get("delta", {}) if isinstance(choice,dict) else {}
                if isinstance(delta,dict) and isinstance(delta.get("content"),str): parts.append(delta["content"])
        text="".join(parts)
        if not text: text="".join(envelope.get("text_parts", []))
        svg=extract_svg(text)
        label, confidence, details=classifier.classify(svg)
        good=label=="good" and confidence>=THRESHOLD
        safe_log("pelican_probe", account_id=int(account_id), label=label, confidence=round(confidence,4), good=good, elapsed_ms=int((time.monotonic()-started)*1000), username_hash=proxy_username[-12:])
        return good, label, confidence, details
    except APIError as exc:
        safe_log("pelican_probe_failed", account_id=int(account_id), elapsed_ms=int((time.monotonic()-started)*1000), error=type(exc).__name__, api_status=exc.status, api_code=exc.code)
        return False, str(exc.code or "failed"), 0.0, {}
    except Exception as exc:
        safe_log("pelican_probe_failed", account_id=int(account_id), elapsed_ms=int((time.monotonic()-started)*1000), error=type(exc).__name__)
        return False, "failed", 0.0, {}

def main():
    client=GrokClient(); client.login(); client.ensure_build_account_proxy(NODE_ID); classifier=KNNClassifier.from_snapshot(Path(os.environ.get("PELICAN_MODEL_PATH", "/app/model/pelican-knn-v1.json")))
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
                        good,label,conf,_=probe(client,classifier,aid,name)
                        if good and not exit_ip:
                            # A transient trace failure before generation
                            # must not admit an IP-less good entry. Retry the
                            # trace after the model request has completed.
                            exit_ip=trace_ip(name)
                        if good and not exit_ip:
                            safe_log("pelican_good_without_exit_ip", username_hash=name[-12:])
                            continue
                        client.admin_request("POST", "/api/admin/v1/pelican-egress-pool/results", {"proxy_username":name,"exit_ip":exit_ip,"label":label,"confidence":conf,"classifier_version":"pelican-knn-v1"})
                        break
            time.sleep(3 if len(pool) < POOL_TARGET else 30)
        except Exception as exc:
            safe_log("pelican_pool_cycle_failed", error=type(exc).__name__); time.sleep(30)

if __name__ == "__main__": main()
