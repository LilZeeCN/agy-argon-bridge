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
import asyncio
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
VERSION = "1.3.3"
last_response = None

try:
    import h2.exceptions
    H2_EXCEPTIONS = (h2.exceptions.H2Error,)
except ImportError:
    H2_EXCEPTIONS = ()

@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        await client.aio.aclose()

app = FastAPI(
    title="Antigravity Gemini 4 Argon Bridge",
    description="Bridge Antigravity CLI to Google Cloud Vertex AI Gemini 4 Argon with Full Tool Calling Support and Resilient Auto-Retry",
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
            timeout=10,
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

TIMEOUT_MS = int(os.environ.get("ARGON_BRIDGE_TIMEOUT_MS", 1200000))  # 默认 20 分钟 (1,200,000 ms)

def create_genai_client():
    return genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION,
        http_options=types.HttpOptions(
            timeout=TIMEOUT_MS,
            async_client_args={"http2": True},
            client_args={"http2": True},
        ),
    )

client = create_genai_client()

async def reset_client():
    """在底层 HTTP/2 连接损坏（如长时间运行后连接被远端关闭）时安全重置客户端连接池"""
    global client
    try:
        await client.aio.aclose()
    except Exception:
        pass
    client = create_genai_client()

def is_retryable_error(exc: Exception) -> bool:
    """判断异常是否为可重试的临时性限流、云端超时、断网或底层 HTTP/2 状态机断连错误"""
    code = getattr(exc, "code", None)
    if code in (429, 503, 504):
        return True
    if isinstance(exc, (httpx.TimeoutException, TimeoutError, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProtocolError, *H2_EXCEPTIONS)):
        return True
    err_msg = str(exc).upper()
    if any(kw in err_msg for kw in (
        "RESOURCE_EXHAUSTED", "429", "TOO MANY REQUESTS", "DEADLINE_EXCEEDED",
        "UNAVAILABLE", "TIMEOUT", "SERVER DISCONNECTED", "CONNECTION CLOSED",
        "CONNECTIONSTATE", "RECV_PING", "STREAM_CLOSED", "GOAWAY", "H2", "PROTOCOLERROR",
        "PEER CLOSED", "INCOMPLETE CHUNKED READ"
    )):
        return True
    return False

async def execute_with_backoff(func, *args, max_retries=5, **kwargs):
    """通用异步重试包装器，处理非流式调用的临时限流与网络抖动"""
    delays = [5, 10, 15, 25, 40]
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if is_retryable_error(e) and attempt < max_retries:
                await reset_client()
                wait_sec = delays[min(attempt, len(delays) - 1)]
                print(f"[RATE_LIMIT_GUARD] Transient error {e}. Reset client connection pool and backoff {wait_sec}s (retry {attempt + 1}/{max_retries})...", flush=True)
                await asyncio.sleep(wait_sec)
            else:
                raise

def sanitize_chunk(obj):
    """
    核心兼容层：清洗响应数据，确保符合 Go 语言客户端 (agy) 的反序列化要求。
    
    【关键痛点与原理】：
    1. SDK 的 JSON 序列化可能将 `thoughtSignature` 表示为 URL-Safe Base64 (含 '-' 和 '_')。
       Go 标准库 `json.Unmarshal` 对 `[]uint8` ([]byte) 字段反序列化时，仅接受标准 Base64 (含 '+' 和 '/')，
       否则直接抛出 `illegal base64 data at input byte 6`。
    2. 工具调用 (`functionCall` / `functionResponse`)：
       确保 `name` 字段存在且优先、`args` 为合法字典，规避 Go 语言 `mapToStruct` 在超大载荷下的结构体映射崩溃。
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
            elif k == "inlineData" and isinstance(v, dict):
                blob = sanitize_chunk(v)
                encoded = blob.get("data")
                if isinstance(encoded, str) and encoded:
                    try:
                        padded = encoded + "=" * (-len(encoded) % 4)
                        blob["data"] = base64.b64encode(base64.urlsafe_b64decode(padded)).decode("ascii")
                    except Exception as e:
                        print(f"[WARN] Failed to convert inlineData: {e}", file=sys.stderr)
                new_dict[k] = blob
            elif k == "functionCall" and isinstance(v, dict):
                fc = sanitize_chunk(v)
                name = fc.get("name")
                if not isinstance(name, str) or not name:
                    name = "unknown_tool"
                args = fc.get("args")
                if not isinstance(args, dict):
                    args = {}
                clean_fc = {"name": name, "args": args}
                if "id" in fc and fc["id"]:
                    clean_fc["id"] = str(fc["id"])
                new_dict[k] = clean_fc
            elif k == "functionResponse" and isinstance(v, dict):
                fr = sanitize_chunk(v)
                name = fr.get("name")
                if not isinstance(name, str) or not name:
                    name = "unknown_tool"
                resp = fr.get("response")
                if not isinstance(resp, dict):
                    resp = {}
                clean_fr = {"name": name, "response": resp}
                if "id" in fr and fr["id"]:
                    clean_fr["id"] = str(fr["id"])
                new_dict[k] = clean_fr
            else:
                new_dict[k] = sanitize_chunk(v)
        return new_dict
    elif isinstance(obj, list):
        return [sanitize_chunk(item) for item in obj]
    else:
        return obj

def sanitize_conversation_history(raw_contents: list) -> list:
    """
    清洗对话上下文历史，防止死循环和畸形数据导致 Vertex AI 掐断连接：
    1. 剔除 agy Go 校验引擎注入的错误重试消息（如 model output error...）
    2. 严格按规范校准角色：functionCall 必须为 model，functionResponse 必须为 user
    3. 严禁在一个轮次中同时混入 functionCall 和 functionResponse（自动拆分为独立轮次）
    4. 严格合并连续纯文本轮次，但隔离 functionResponse 与 functionCall，避免破坏协议校验
    5. 确保历史以 user 轮次开头，以 user 轮次结尾
    """
    if not raw_contents:
        return []

    # 第一阶段：拆解并按 part 正确归类角色
    normalized_turns = []
    for c in raw_contents:
        if not isinstance(c, dict):
            continue
        original_role = c.get("role", "user")
        parts = c.get("parts")
        if not isinstance(parts, list):
            continue

        model_parts = []
        user_parts = []
        for p in parts:
            if not isinstance(p, dict):
                continue

            text = p.get("text")
            if isinstance(text, str):
                if "model output must contain either output text or tool calls" in text:
                    continue
                if not text.strip() and not p.get("thought") and len(parts) == 1 and original_role == "model":
                    continue

            if not any(k in p for k in ("functionCall", "functionResponse", "inlineData", "text")):
                continue

            if "functionCall" in p:
                model_parts.append(p)
            elif "functionResponse" in p:
                user_parts.append(p)
            else:
                if original_role == "model":
                    model_parts.append(p)
                else:
                    user_parts.append(p)

        if original_role == "model":
            if model_parts:
                normalized_turns.append({"role": "model", "parts": model_parts})
            if user_parts:
                normalized_turns.append({"role": "user", "parts": user_parts})
        else:
            if user_parts:
                normalized_turns.append({"role": "user", "parts": user_parts})
            if model_parts:
                normalized_turns.append({"role": "model", "parts": model_parts})

    if not normalized_turns:
        return []

    # 第二阶段：严格轮流与安全合并
    cleaned = []
    for turn in normalized_turns:
        role = turn["role"]
        parts = turn["parts"]

        if role == "model" and not any("functionCall" in p or (p.get("text") and not p.get("thought")) for p in parts):
            parts.append({"text": "[Step executed]"})

        if not cleaned:
            cleaned.append({"role": role, "parts": parts})
            continue

        prev_role = cleaned[-1]["role"]
        if prev_role == role:
            prev_has_fc = any("functionCall" in p for p in cleaned[-1]["parts"])
            curr_has_fc = any("functionCall" in p for p in parts)
            prev_has_fr = any("functionResponse" in p for p in cleaned[-1]["parts"])
            curr_has_fr = any("functionResponse" in p for p in parts)

            can_merge = False
            if role == "model" and not prev_has_fr and not curr_has_fr:
                can_merge = True
            elif role == "user" and not prev_has_fc and not curr_has_fc:
                if not prev_has_fr and not curr_has_fr:
                    can_merge = True
                elif prev_has_fr and curr_has_fr:
                    can_merge = True

            if can_merge:
                cleaned[-1]["parts"].extend(parts)
            else:
                if role == "user":
                    cleaned.append({"role": "model", "parts": [{"text": "[Acknowledged]"}]})
                else:
                    cleaned.append({"role": "user", "parts": [{"text": "Continue"}]})
                cleaned.append({"role": role, "parts": parts})
        else:
            cleaned.append({"role": role, "parts": parts})

    while cleaned and cleaned[0].get("role") != "user":
        cleaned.pop(0)

    if cleaned and cleaned[-1].get("role") == "model":
        cleaned.append({"role": "user", "parts": [{"text": "Please continue the task."}]})

    return cleaned

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
        # 兼容处理思考强度：将客户端传入的 budget 映射为 Vertex AI 的 thinking_level，防止配置被丢弃
        if cfg.thinking_config:
            budget = cfg.thinking_config.thinking_budget
            if cfg.thinking_config.thinking_level is None:
                if budget is not None:
                    if budget <= 0:
                        cfg.thinking_config.thinking_level = "HIGH"
                    elif budget <= 2000:
                        cfg.thinking_config.thinking_level = "LOW"
                    elif budget <= 6000:
                        cfg.thinking_config.thinking_level = "MEDIUM"
                    else:
                        cfg.thinking_config.thinking_level = "HIGH"
                else:
                    cfg.thinking_config.thinking_level = "HIGH"
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
    
    # 核心净化：剔除此前死循环产生的报错噪音与空轮次，修复结构严格符合 Vertex AI 规范
    clean_contents = sanitize_conversation_history(raw_contents)

    # 解析对话历史与工具响应结果
    parsed_contents = []
    for c in clean_contents:
        try:
            parsed_contents.append(types.Content.model_validate(c))
        except Exception:
            parsed_contents.append(c)

    has_tools = bool(body.get("tools"))
    print(f"\n==================== [AGY REQUEST] ====================")
    print(f"Path: {model_path}")
    print(f"Has Local Tools: {has_tools} (Declarations count: {len(body.get('tools', []))})")
    print(f"Contents count: raw={len(raw_contents)} -> sanitized={len(parsed_contents)}")
    print(f"Routing -> Google Cloud Vertex AI ({upstream_model})", flush=True)
    print(f"=======================================================\n")

    # 1. 处理 Token 计算
    if operation == "countTokens":
        try:
            res = await execute_with_backoff(
                client.aio.models.count_tokens,
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

    # 2. 处理流式输出 (流式返回并实施 Base64 清洗、429 指数退避与 SSE 注释保活)
    if is_stream:
        max_retries = 6
        delays = [10, 15, 20, 30, 45, 60]  # 阶梯退避宽限长达 3 分钟，彻底消化 GCP 60s TPM 限流窗口
        response_stream = None
        first_chunk = None

        for attempt in range(max_retries + 1):
            try:
                response_stream = await client.aio.models.generate_content_stream(
                    model=upstream_model, contents=parsed_contents, config=cfg,
                )
                first_chunk = await asyncio.wait_for(anext(response_stream), timeout=60.0)
                break
            except Exception as e:
                if is_retryable_error(e) and attempt < max_retries:
                    await reset_client()
                    wait_sec = delays[min(attempt, len(delays) - 1)]
                    print(f"\n[RATE_LIMIT_GUARD] Upstream {upstream_model} hit rate limit / temporary error: {e}", flush=True)
                    print(f"[RATE_LIMIT_GUARD] Reset HTTP/2 client connection pool. Entering silent backoff: waiting {wait_sec}s (retry {attempt + 1}/{max_retries})...", flush=True)
                    await asyncio.sleep(wait_sec)
                else:
                    return upstream_error(e)

        async def event_generator():
            has_emitted_content = False
            try:
                first_dict = response_dict(first_chunk, requested_model, upstream_model)
                for cand in first_dict.get("candidates", []):
                    for p in cand.get("content", {}).get("parts", []):
                        if (p.get("text") and not p.get("thought")) or "functionCall" in p:
                            has_emitted_content = True
                yield f"data: {json.dumps(first_dict)}\n\n"

                async for chunk in response_stream:
                    chunk_dict = response_dict(chunk, requested_model, upstream_model)
                    for cand in chunk_dict.get("candidates", []):
                        for p in cand.get("content", {}).get("parts", []):
                            if (p.get("text") and not p.get("thought")) or "functionCall" in p:
                                has_emitted_content = True
                    yield f"data: {json.dumps(chunk_dict)}\n\n"
            except Exception as e:
                print(f"Streaming error during stream iteration on {upstream_model}: {e}", file=sys.stderr, flush=True)
                if not has_emitted_content:
                    fallback_dict = {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "text": "⚠️ [流式传输因网络抖动中断，已保留当前思考进度。请在终端重新输入指令继续]"
                                        }
                                    ],
                                },
                                "finishReason": "STOP",
                            }
                        ]
                    }
                    yield f"data: {json.dumps(fallback_dict)}\n\n"
                else:
                    err_dict = {"error": {"message": str(e), "code": 500}}
                    yield f"data: {json.dumps(err_dict)}\n\n"
            finally:
                await response_stream.aclose()

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # 3. 处理非流式输出
    else:
        try:
            resp = await execute_with_backoff(
                client.aio.models.generate_content,
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
    uvicorn.run(app, host="127.0.0.1", port=port, timeout_keep_alive=600)
