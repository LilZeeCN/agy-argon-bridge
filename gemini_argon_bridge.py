#!/usr/bin/env python3
"""
agy-argon-bridge: Antigravity CLI to Google Cloud Vertex AI (Gemini 4 Argon) Bridge
Author: LilZee (https://github.com/LilZeeCN)
License: MIT
"""

import os
import sys
import json
import base64
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import google.auth
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn
from google import genai
from google.genai import types
from pydantic import ValidationError

TARGET_MODEL = "gemini-4-argon"
VERSION = "1.1.0"
last_response = None

@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        await client.aio.aclose()

app = FastAPI(
    title="Antigravity Gemini 4 Argon Bridge",
    description="Bridge Antigravity CLI to Google Cloud Vertex AI Gemini 4 Argon with Full Tool Calling Support",
    version=VERSION,
    lifespan=lifespan,
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
    credentials, project = google.auth.default()
    project = project or credentials.quota_project_id
    if project:
        return project
    raise RuntimeError("无法确定 Google Cloud 项目，请设置 GOOGLE_CLOUD_PROJECT 后重新安装。")

PROJECT_ID = get_default_project_id()
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")

# 初始化 Vertex AI 客户端，使用本机 gcloud ADC 凭据
client = genai.Client(
    vertexai=True,
    project=PROJECT_ID,
    location=LOCATION,
    http_options=types.HttpOptions(timeout=120000),
)

def sanitize_chunk(obj):
    """
    核心兼容层：清洗响应数据，确保符合 Go 语言客户端 (agy) 的反序列化要求。
    
    【关键痛点与原理】：
    SDK 的 JSON 序列化可能将 `thoughtSignature` 表示为 URL-Safe Base64 (含 '-' 和 '_')。
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
        "status": "ready",
        "bridge": "agy-argon-bridge",
        "version": VERSION,
        "model": TARGET_MODEL,
        "project": PROJECT_ID,
        "location": LOCATION,
        "last_response": last_response,
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

def build_config(body: dict, model: str = TARGET_MODEL) -> types.GenerateContentConfig:
    config_args = dict(body.get("generationConfig") or body.get("generation_config") or {})

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

    cfg = types.GenerateContentConfig.model_validate(config_args)
    if model == TARGET_MODEL:
        # Argon 不支持旧模型的 token 推理预算；保留明确级别，旧预算使用默认 MEDIUM。
        if cfg.thinking_config:
            cfg.thinking_config.thinking_budget = None
            if cfg.thinking_config.thinking_level == "MINIMAL":
                cfg.thinking_config.thinking_level = "LOW"
        cfg.temperature = cfg.top_p = cfg.top_k = None
    return cfg

def response_dict(response, requested_model, upstream_model):
    global last_response
    data = response.model_dump(by_alias=True, mode="json", exclude_none=True)
    data.pop("sdkHttpResponse", None)
    if data.get("modelVersion"):
        last_response = {
            "at": datetime.now(timezone.utc).isoformat(),
            "requested_model": requested_model,
            "upstream_model": upstream_model,
            "modelVersion": data["modelVersion"],
            "responseId": data.get("responseId"),
        }
        if data.get("usageMetadata"):
            last_response["usageMetadata"] = data["usageMetadata"]
        print(json.dumps({"event": "upstream_response", **last_response}), flush=True)
    return sanitize_chunk(data)

def upstream_error(exc):
    code = getattr(exc, "code", 500)
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        code = 504
    if not isinstance(code, int) or not 400 <= code <= 599:
        code = 502
    return JSONResponse(status_code=code, content={"error": {"message": str(exc), "code": code}})

@app.post("/v1beta/models/{model_path:path}")
@app.post("/v1/models/{model_path:path}")
async def handle_model_request(model_path: str, request: Request):
    requested_model, separator, operation = model_path.rpartition(":")
    if not separator or operation not in {"countTokens", "generateContent", "streamGenerateContent"}:
        return JSONResponse(status_code=404, content={"error": {"message": "Unsupported model operation"}})
    # 图片模型保留原模型，其余 Gemini 文本请求映射到 Argon。
    upstream_model = requested_model if "image" in requested_model else TARGET_MODEL
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
    print(f"Routing -> Google Cloud Vertex AI ({upstream_model})", flush=True)
    print(f"=======================================================\n")

    # 1. 处理 Token 计算
    if operation == "countTokens":
        try:
            res = await client.aio.models.count_tokens(
                model=upstream_model,
                contents=parsed_contents,
                config=types.CountTokensConfig(
                    system_instruction=body.get("systemInstruction") or body.get("system_instruction"),
                    tools=body.get("tools"),
                ),
            )
            return JSONResponse(content={"totalTokens": res.total_tokens})
        except Exception as e:
            return upstream_error(e)

    try:
        cfg = build_config(body, upstream_model)
    except ValidationError as e:
        return JSONResponse(status_code=400, content={"error": {"message": str(e), "code": 400}})
    is_stream = operation == "streamGenerateContent"

    # 2. 处理流式输出 (流式返回并实施 Base64 清洗)
    if is_stream:
        # 在发送 HTTP 200 前取得首帧，鉴权、配额和网络失败保留正确状态码。
        try:
            response_stream = await client.aio.models.generate_content_stream(
                model=upstream_model, contents=parsed_contents, config=cfg,
            )
            first_chunk = await anext(response_stream)
        except Exception as e:
            return upstream_error(e)

        async def event_generator():
            try:
                yield f"data: {json.dumps(response_dict(first_chunk, requested_model, upstream_model))}\n\n"
                async for chunk in response_stream:
                    chunk_dict = response_dict(chunk, requested_model, upstream_model)
                    yield f"data: {json.dumps(chunk_dict)}\n\n"
            except Exception as e:
                print(f"Streaming error on {upstream_model}: {e}", file=sys.stderr)
                err_dict = {"error": {"message": str(e), "code": 500}}
                yield f"data: {json.dumps(err_dict)}\n\n"
            finally:
                await response_stream.aclose()

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # 3. 处理非流式输出
    else:
        try:
            resp = await client.aio.models.generate_content(
                model=upstream_model,
                contents=parsed_contents,
                config=cfg,
            )
            resp_dict = response_dict(resp, requested_model, upstream_model)
            return JSONResponse(content=resp_dict)
        except Exception as e:
            print(f"GenerateContent error on {upstream_model}: {e}", file=sys.stderr)
            return upstream_error(e)

if __name__ == "__main__":
    port = int(os.environ.get("ARGON_BRIDGE_PORT", 8050))
    print(f"Starting agy-argon-bridge on 127.0.0.1:{port} (Project: {PROJECT_ID})...")
    uvicorn.run(app, host="127.0.0.1", port=port)
