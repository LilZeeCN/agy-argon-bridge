#!/usr/bin/env python3
"""
agy-argon-bridge: Antigravity CLI to Google Cloud Vertex AI (Gemini 4 Argon) Bridge
Author: LilZee (https://github.com/LilZeeCN)
License: MIT
"""

import os
import sys
import json
import time
import base64
import subprocess
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn
from google import genai
from google.genai import types

app = FastAPI(
    title="Antigravity Gemini 4 Argon Bridge",
    description="Bridge Antigravity CLI to Google Cloud Vertex AI Gemini 4 Argon with Full Tool Calling Support",
    version="1.0.0",
)

def get_default_project_id() -> str:
    """自动获取当前活跃的 Google Cloud 项目 ID"""
    env_proj = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if env_proj:
        return env_proj
    try:
        res = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0 and res.stdout.strip() and "(unset)" not in res.stdout:
            return res.stdout.strip()
    except Exception:
        pass
    # 默认 fallback
    return "gen-lang-client-0567634324"

PROJECT_ID = get_default_project_id()
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")

# 初始化 Vertex AI 客户端，使用本机 gcloud ADC 凭据
client = genai.Client(
    vertexai=True,
    project=PROJECT_ID,
    location=LOCATION,
)

def sanitize_chunk(obj):
    """
    核心兼容层：清洗响应数据，确保符合 Go 语言客户端 (agy) 的反序列化要求。
    
    【关键痛点与原理】：
    Vertex AI 返回的 `thoughtSignature` 采用 RFC 4648 URL-Safe Base64 编码 (含 '-' 和 '_')。
    而 Antigravity CLI 客户端采用 Go 语言实现，Go 标准库 `json.Unmarshal` 对 `[]uint8` ([]byte)
    字段反序列化时，仅接受标准 Base64 格式 (含 '+' 和 '/')，否则会直接抛出：
    `json: cannot unmarshal string into Go struct field ... illegal base64 data at input byte 6`。
    
    本函数自动将所有层级的 `thoughtSignature` 补齐合法 padding 并转为标准 Base64 编码，
    彻底保障本地工具调用的顺畅回传。
    """
    if isinstance(obj, dict):
        new_dict = {}
        for k, v in obj.items():
            if k == "thoughtSignature":
                if isinstance(v, str) and v:
                    try:
                        padded = v + "=" * (-len(v) % 4)
                        raw_bytes = base64.urlsafe_b64decode(padded)
                        new_dict[k] = base64.b64encode(raw_bytes).decode("ascii")
                    except Exception as e:
                        print(f"[WARN] Failed to convert thoughtSignature: {e}", file=sys.stderr)
                        pass
            else:
                new_dict[k] = sanitize_chunk(v)
        return new_dict
    elif isinstance(obj, list):
        return [sanitize_chunk(item) for item in obj]
    else:
        return obj

@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "model": "gemini-4-argon",
        "project": PROJECT_ID,
        "location": LOCATION,
    }

@app.get("/v1beta/models")
@app.get("/v1/models")
async def list_models():
    """提供与 Gemini API 兼容的模型列表，让 agy 识别模型可用性"""
    return {
        "models": [
            {
                "name": "models/gemini-4-argon",
                "displayName": "Gemini 4 Argon",
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent", "countTokens"],
            },
            {
                "name": "models/gemini-3.8-flash",
                "displayName": "Gemini 3.8 Flash",
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent", "countTokens"],
            },
            {
                "name": "models/gemini-3.1-flash-lite-preview",
                "displayName": "Gemini 3.1 Flash Lite Preview",
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent", "countTokens"],
            }
        ]
    }

def build_config(body: dict) -> types.GenerateContentConfig:
    config_args = {}

    # 1. 保留客户端注入的系统角色与工作流指令
    sys_inst = body.get("systemInstruction") or body.get("system_instruction")
    if sys_inst:
        config_args["system_instruction"] = sys_inst

    # 2. 核心：透传 agy 客户端声明的所有本地工具（run_command, view_file, write_to_file 等）
    tools = body.get("tools")
    if tools:
        config_args["tools"] = tools

    # 3. 工具调用模式配置
    tool_cfg = body.get("toolConfig") or body.get("tool_config")
    if tool_cfg:
        config_args["tool_config"] = tool_cfg

    # 4. 安全过滤设置
    safety = body.get("safetySettings") or body.get("safety_settings")
    if safety:
        config_args["safety_settings"] = safety

    # 5. 生成配置
    gen_cfg = body.get("generationConfig", {})
    if "temperature" in gen_cfg:
        config_args["temperature"] = gen_cfg["temperature"]
    if "topP" in gen_cfg:
        config_args["top_p"] = gen_cfg["topP"]
    if "topK" in gen_cfg:
        config_args["top_k"] = gen_cfg["topK"]
    if "maxOutputTokens" in gen_cfg:
        config_args["max_output_tokens"] = gen_cfg["maxOutputTokens"]
    if "stopSequences" in gen_cfg:
        config_args["stop_sequences"] = gen_cfg["stopSequences"]

    return types.GenerateContentConfig(**config_args)

@app.post("/v1beta/models/{model_path:path}")
@app.post("/v1/models/{model_path:path}")
async def handle_model_request(model_path: str, request: Request):
    body = await request.json()
    raw_contents = body.get("contents", [])
    
    # 解析对话历史与工具响应结果
    parsed_contents = []
    for c in raw_contents:
        try:
            parsed_contents.append(types.Content.model_validate(c))
        except Exception:
            parsed_contents.append(c)

    has_tools = bool(body.get("tools"))
    print(f"\n==================== [AGY REQUEST] ====================")
    print(f"Path: {model_path}")
    print(f"Has Local Tools: {has_tools} (Declarations count: {len(body.get('tools', []))})")
    print(f"Contents count: {len(parsed_contents)}")
    print(f"Routing -> Google Cloud Vertex AI (gemini-4-argon)")
    print(f"=======================================================\n")

    # 1. 处理 Token 计算
    if ":countTokens" in model_path:
        try:
            res = client.models.count_tokens(
                model="gemini-4-argon",
                contents=parsed_contents,
            )
            return JSONResponse(content={"totalTokens": res.total_tokens})
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": {"message": str(e)}})

    cfg = build_config(body)
    is_stream = ":streamGenerateContent" in model_path

    # 2. 处理流式输出 (流式返回并实施 Base64 清洗)
    if is_stream:
        async def event_generator():
            try:
                response_stream = client.models.generate_content_stream(
                    model="gemini-4-argon",
                    contents=parsed_contents,
                    config=cfg,
                )
                for chunk in response_stream:
                    chunk_dict = chunk.model_dump(by_alias=True, mode="json", exclude_none=True)
                    chunk_dict.pop("sdkHttpResponse", None)
                    chunk_dict = sanitize_chunk(chunk_dict)
                    yield f"data: {json.dumps(chunk_dict)}\n\n"
            except Exception as e:
                print(f"Streaming error on gemini-4-argon: {e}", file=sys.stderr)
                err_dict = {"error": {"message": str(e), "code": 500}}
                yield f"data: {json.dumps(err_dict)}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # 3. 处理非流式输出
    else:
        try:
            resp = client.models.generate_content(
                model="gemini-4-argon",
                contents=parsed_contents,
                config=cfg,
            )
            resp_dict = resp.model_dump(by_alias=True, mode="json", exclude_none=True)
            resp_dict.pop("sdkHttpResponse", None)
            resp_dict = sanitize_chunk(resp_dict)
            return JSONResponse(content=resp_dict)
        except Exception as e:
            print(f"GenerateContent error on gemini-4-argon: {e}", file=sys.stderr)
            return JSONResponse(status_code=500, content={"error": {"message": str(e)}})

if __name__ == "__main__":
    port = int(os.environ.get("ARGON_BRIDGE_PORT", 8050))
    print(f"Starting agy-argon-bridge on 127.0.0.1:{port} (Project: {PROJECT_ID})...")
    uvicorn.run(app, host="127.0.0.1", port=port)
