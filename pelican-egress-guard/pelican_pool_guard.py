#!/usr/bin/env python3
"""Maintains the fixed-size Build Resin good-username pool."""
from __future__ import annotations
import os, random, secrets, time, datetime as dt
from pathlib import Path
from guard import APIError, GrokClient, KNNClassifier, extract_svg, http_sse, safe_log, now_iso, MODEL

PROMPT = "画一个鹈鹕骑自行车的svg"
THRESHOLD = 0.60
INTERVAL = 600
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

def due(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() <= time.time()
    except (TypeError, ValueError, OverflowError):
        return True

def probe(client, classifier, account_id, proxy_username):
    body = {"provider":"grok_build", "account_id":int(account_id), "egress_node_id":NODE_ID, "proxy_username":proxy_username,
            "request":{"model":MODEL, "stream":True, "messages":[{"role":"user","content":PROMPT}]}}
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
    client=GrokClient(); client.login(); classifier=KNNClassifier.from_snapshot(Path(os.environ.get("PELICAN_MODEL_PATH", "/app/model/pelican-knn-v1.json")))
    while True:
        try:
            pool=client.admin_request("GET", "/api/admin/v1/pelican-egress-pool").get("items", [])
            due_items=[x for x in pool if due(x.get("next_check_at", x.get("nextCheckAt", x.get("NextCheckAt", ""))))]
            if due_items:
                item=due_items[0]; name=item.get("proxy_username", item.get("proxyUsername", item.get("ProxyUsername"))); aid=random.choice(accounts(client)).get("id")
                good,label,conf,_=probe(client,classifier,aid,name)
                client.admin_request("POST", "/api/admin/v1/pelican-egress-pool/results", {"proxy_username":name,"label":label,"confidence":conf,"classifier_version":"pelican-knn-v1"})
            elif len(pool)<3:
                choices=accounts(client)
                if choices:
                    aid=random.choice(choices).get("id"); name=username(); good,label,conf,_=probe(client,classifier,aid,name)
                    if good: client.admin_request("POST", "/api/admin/v1/pelican-egress-pool/results", {"proxy_username":name,"label":"good","confidence":conf,"classifier_version":"pelican-knn-v1"})
            time.sleep(3 if len(pool)<3 else 30)
        except Exception as exc:
            safe_log("pelican_pool_cycle_failed", error=type(exc).__name__); time.sleep(30)

if __name__ == "__main__": main()
