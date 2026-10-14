#!/usr/bin/env python3
"""验证本地桥接的真实云端响应；此命令会产生少量 API 用量。"""
import argparse
import json
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--port", type=int, default=8050)
args = parser.parse_args()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
body = {"contents": [{"role": "user", "parts": [{"text": "Reply exactly ARGON_VERIFIED."}]}],
        "generationConfig": {"maxOutputTokens": 256, "thinkingConfig": {"thinkingLevel": "LOW"}}}
request = urllib.request.Request(
    f"http://127.0.0.1:{args.port}/v1beta/models/gemini-4-argon:generateContent",
    data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
)
try:
    with opener.open(request, timeout=130) as response:
        data = json.load(response)
except urllib.error.HTTPError as error:
    print(error.read().decode())
    raise SystemExit(1)
result = {key: data.get(key) for key in ("modelVersion", "responseId", "usageMetadata")}
result["text"] = [part["text"] for candidate in data.get("candidates", [])
                  for part in candidate.get("content", {}).get("parts", [])
                  if part.get("text") and not part.get("thought")]
result["billing_credit_deduction"] = "Not verified; check Cloud Billing credits and cost details."
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result["modelVersion"] == "gemini-4-argon" else 1)
