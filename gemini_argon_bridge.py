#!/usr/bin/env python3
"""
agy-argon-bridge: Antigravity CLI to Google Cloud Vertex AI (Gemini 4 Carbon / Argon) Bridge
Author: LilZee (https://github.com/LilZeeCN)
License: MIT
"""

import os
import sys
import json
import base64
import shutil
import subprocess
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import google.auth
from google.oauth2 import credentials as oauth2_credentials
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn
from google import genai
from google.genai import types
from pydantic import ValidationError

TARGET_MODEL = os.environ.get("ARGON_BRIDGE_TARGET_MODEL") or os.environ.get("TARGET_MODEL") or "gemini-4-carbon"
FALLBACK_MODEL = os.environ.get(
    "ARGON_BRIDGE_FALLBACK_MODEL",
    "gemini-4-argon" if TARGET_MODEL == "gemini-4-carbon" else "gemini-4-carbon",
)
AUTO_COMPACT_CHAR_LIMIT = int(os.environ.get("ARGON_AUTO_COMPACT_CHAR_LIMIT", 180000))  # 约 4.5w~6w tokens 触发瘦身
AUTO_COMPACT_RECENT_TURNS = int(os.environ.get("ARGON_AUTO_COMPACT_RECENT_TURNS", 12))
VERSION = "1.5.0"
last_response = None

try:
    import h2.exceptions
    H2_EXCEPTIONS = (h2.exceptions.H2Error,)
except ImportError:
    H2_EXCEPTIONS = ()

LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
TIMEOUT_MS = int(os.environ.get("ARGON_BRIDGE_TIMEOUT_MS", 1200000))  # 默认 20 分钟 (1,200,000 ms)
POOL_CONFIG_PATH = os.environ.get(
    "ARGON_ACCOUNTS_POOL",
    os.path.expanduser("~/.local/share/agy-argon/accounts_pool.json"),
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
    project = project or getattr(credentials, "quota_project_id", None)
    if project:
        return project
    raise RuntimeError("无法确定 Google Cloud 项目，请设置 GOOGLE_CLOUD_PROJECT 后重新安装。")


def _build_http_options() -> types.HttpOptions:
    return types.HttpOptions(
        timeout=TIMEOUT_MS,
        async_client_args={"http2": True},
        client_args={"http2": True},
    )


class AccountPoolManager:
    """
    多账号池与本地 Token 实时计费无感轮转管理器：
    - 预留 $15 安全缓冲（单号 $300 赠金使用上限 $285.00）。
    - 基于每次响应的 usageMetadata 实时精准计算美元消耗（按 responseId 去重计算差值）。
    - 当活跃账号达到 $285.00 阈值时，毫秒级无感切换至下一个有额度的账号，并对已达上限账号执行硬性熔断。
    - 当高并发子代理触发 429 限流时，支持自动跨账号分流重试。
    """

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.accounts = []
        self.clients = {}
        self.active_email = None
        self.safety_buffer_usd = 15.0
        self.pricing = {
            "prompt_usd": 2.5,
            "cached_prompt_usd": 0.5,
            "output_and_thought_usd": 15.0,
        }
        self._seen_response_costs = OrderedDict()
        self._load_pool()

    def _create_account_client(self, acc: dict):
        cred_file = os.path.expanduser(acc["credentials_file"])
        if os.path.exists(cred_file):
            creds = oauth2_credentials.Credentials.from_authorized_user_file(
                cred_file,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            return genai.Client(
                vertexai=True,
                project=acc["project_id"],
                location=LOCATION,
                credentials=creds,
                http_options=_build_http_options(),
            )
        return None

    def _load_pool(self):
        if not os.path.exists(self.config_path):
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.safety_buffer_usd = float(data.get("safety_buffer_usd", 15.0))
            if "pricing_per_1m_tokens" in data:
                self.pricing.update(data["pricing_per_1m_tokens"])
            self.active_email = data.get("active_account")
            self.accounts = data.get("accounts", [])

            for acc in self.accounts:
                if not acc.get("enabled", True):
                    continue
                c = self._create_account_client(acc)
                if c is not None:
                    self.clients[acc["email"]] = c
            self._ensure_valid_active_account()
        except Exception as e:
            print(f"[WARN] Failed to load accounts pool from {self.config_path}: {e}", file=sys.stderr)

    def _save_pool(self):
        if not self.accounts or not os.path.exists(os.path.dirname(self.config_path)):
            return
        try:
            payload = {
                "safety_buffer_usd": self.safety_buffer_usd,
                "pricing_per_1m_tokens": self.pricing,
                "active_account": self.active_email,
                "accounts": self.accounts,
            }
            tmp_path = self.config_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.config_path)
        except Exception as e:
            print(f"[WARN] Failed to save accounts pool: {e}", file=sys.stderr)

    def _get_account_dict(self, email: str):
        for acc in self.accounts:
            if acc["email"] == email:
                return acc
        return None

    def _is_account_available(self, acc: dict) -> bool:
        if not acc or not acc.get("enabled", True):
            return False
        if acc["email"] not in self.clients:
            return False
        limit = float(acc.get("limit_usd", acc.get("total_credit_usd", 300.0) - self.safety_buffer_usd))
        return float(acc.get("used_usd", 0.0)) < limit

    def _ensure_valid_active_account(self):
        """确保当前选中的账号未超额；若已超额则无感切换到下一个可用账号"""
        if not self.accounts:
            return
        curr = self._get_account_dict(self.active_email)
        if self._is_account_available(curr):
            return

        prev_email = self.active_email
        for acc in self.accounts:
            if self._is_account_available(acc):
                self.active_email = acc["email"]
                self._sync_default_adc(acc)
                self._save_pool()
                print(
                    f"\n[ACCOUNT_ROTATION] Account {prev_email} reached safety limit ($285 / $15 buffer left). "
                    f"Seamlessly switched active account -> {self.active_email} (Project: {acc['project_id']})!\n",
                    flush=True,
                )
                return

    def _sync_default_adc(self, acc: dict):
        """同步更新系统默认 ADC 文件，保持外部 CLI 与当前活跃账号一致"""
        try:
            src = os.path.expanduser(acc["credentials_file"])
            dst = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
            if os.path.exists(src) and os.path.exists(os.path.dirname(dst)):
                shutil.copyfile(src, dst)
        except Exception:
            pass

    async def reset_account_client(self, email: str):
        """当某个账号的底层 HTTP/2 连接断开时，安全重建该账号的 Client"""
        acc = self._get_account_dict(email)
        if not acc:
            return
        old = self.clients.get(email)
        if old is not None:
            try:
                await old.aio.aclose()
            except Exception:
                pass
        new_c = self._create_account_client(acc)
        if new_c is not None:
            self.clients[email] = new_c

    def get_candidate_clients(self):
        """
        返回按优先级排列的 [(email, project_id, genai_client), ...] 列表。
        主账号排第一，其余未达 $285 阈值的可用账号作为 429 并发分流备选。
        """
        self._ensure_valid_active_account()
        candidates = []
        if self.active_email and self.active_email in self.clients:
            acc = self._get_account_dict(self.active_email)
            if self._is_account_available(acc):
                candidates.append((self.active_email, acc["project_id"], self.clients[self.active_email]))

        for acc in self.accounts:
            email = acc["email"]
            if email != self.active_email and self._is_account_available(acc):
                candidates.append((email, acc["project_id"], self.clients[email]))
        return candidates

    def calculate_cost_usd(self, usage: dict) -> float:
        if not usage:
            return 0.0
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        cached_tokens = int(usage.get("cachedContentTokenCount") or 0)
        billable_prompt = max(0, prompt_tokens - cached_tokens)
        output_tokens = int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)

        cost = (
            (billable_prompt / 1_000_000.0) * self.pricing["prompt_usd"]
            + (cached_tokens / 1_000_000.0) * self.pricing["cached_prompt_usd"]
            + (output_tokens / 1_000_000.0) * self.pricing["output_and_thought_usd"]
        )
        return cost

    def record_usage(self, email: str, response_id: str, usage: dict) -> float:
        """记录单次响应的美元消耗（对流式同 responseId 自动去重计算增量）并触发阈值检查"""
        if not email or not usage:
            return 0.0
        total_cost = self.calculate_cost_usd(usage)
        if total_cost <= 0:
            return 0.0

        key = f"{email}:{response_id}" if response_id else None
        if key:
            prev_cost = self._seen_response_costs.get(key, 0.0)
            delta = max(0.0, total_cost - prev_cost)
            self._seen_response_costs[key] = max(prev_cost, total_cost)
            if len(self._seen_response_costs) > 2000:
                self._seen_response_costs.popitem(last=False)
        else:
            delta = total_cost

        if delta > 0:
            acc = self._get_account_dict(email)
            if acc:
                acc["used_usd"] = round(float(acc.get("used_usd", 0.0)) + delta, 6)
                self._save_pool()
                self._ensure_valid_active_account()
        return total_cost

    def summary(self):
        return {
            "active_account": self.active_email,
            "safety_buffer_usd": self.safety_buffer_usd,
            "accounts": [
                {
                    "email": a["email"],
                    "project_id": a["project_id"],
                    "total_credit_usd": a.get("total_credit_usd", 300.0),
                    "limit_usd": a.get("limit_usd", 285.0),
                    "used_usd": round(float(a.get("used_usd", 0.0)), 4),
                    "remaining_safe_usd": round(
                        max(0.0, float(a.get("limit_usd", 285.0)) - float(a.get("used_usd", 0.0))), 4
                    ),
                    "remaining_total_usd": round(
                        max(0.0, float(a.get("total_credit_usd", 300.0)) - float(a.get("used_usd", 0.0))), 4
                    ),
                    "expires_at": a.get("expires_at"),
                    "active": a["email"] == self.active_email,
                }
                for a in self.accounts
            ],
        }

    async def aclose_all(self):
        for c in self.clients.values():
            try:
                await c.aio.aclose()
            except Exception:
                pass


pool_manager = AccountPoolManager(POOL_CONFIG_PATH)

PROJECT_ID = (
    pool_manager._get_account_dict(pool_manager.active_email)["project_id"]
    if pool_manager.active_email and pool_manager._get_account_dict(pool_manager.active_email)
    else get_default_project_id()
)


def create_genai_client():
    return genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION,
        http_options=_build_http_options(),
    )


_default_client = (
    pool_manager.clients[pool_manager.active_email]
    if pool_manager.active_email and pool_manager.active_email in pool_manager.clients
    else create_genai_client()
)
client = _default_client


async def reset_client(acc_email: str = None):
    """在底层 HTTP/2 连接损坏（如长时间运行后连接被远端关闭）时安全重置客户端连接池"""
    global client, _default_client
    if acc_email and acc_email in pool_manager.clients:
        await pool_manager.reset_account_client(acc_email)
        if acc_email == pool_manager.active_email:
            _default_client = pool_manager.clients[acc_email]
            client = _default_client
        return
    try:
        await client.aio.aclose()
    except Exception:
        pass
    _default_client = create_genai_client()
    client = _default_client


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        await pool_manager.aclose_all()
        try:
            await client.aio.aclose()
        except Exception:
            pass


app = FastAPI(
    title="Antigravity Gemini 4 Carbon/Argon Bridge",
    description="Bridge Antigravity CLI to Google Cloud Vertex AI with Seamless Multi-Account Auto-Rotation and HTTP/2 Self-Healing",
    version=VERSION,
    lifespan=lifespan,
)


def _get_dispatch_targets():
    """
    获取当前可用于派发请求的客户端列表。
    若单元测试通过 patch.object(bridge, 'client', ...) 注入了假客户端，则优先使用注入的 client。
    """
    if client is not _default_client:
        return [(None, PROJECT_ID, client)]
    candidates = pool_manager.get_candidate_clients()
    if candidates:
        return candidates
    return [(None, PROJECT_ID, client)]


def _get_dispatch_routes(upstream_model: str):
    """
    构建（账号 + 模型）多维高可用路由表：
    1. 优先尝试所有可用账号的 primary model（如 gemini-4-carbon）。
    2. 当单账号或全池在 primary model 上触发 429 限流时，无缝切换至同账号的独立配额池 FALLBACK_MODEL（如 gemini-4-argon），实现单号双倍并发/TPM 容量！
    """
    base_targets = _get_dispatch_targets()
    if client is not _default_client:
        return [(email, proj, c, upstream_model) for email, proj, c in base_targets]

    models = [upstream_model]
    if upstream_model in ("gemini-4-carbon", "gemini-4-argon") and FALLBACK_MODEL and FALLBACK_MODEL != upstream_model:
        models.append(FALLBACK_MODEL)

    routes = []
    for m in models:
        for email, proj, c in base_targets:
            routes.append((email, proj, c, m))
    return routes


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


async def execute_with_pool_backoff(method_name: str, *args, max_retries=8, **kwargs):
    """支持多账号 + 双旗舰模型 (Carbon/Argon) 429 零等待故障转移与 HTTP/2 连接池重置指数退避的异步调用器"""
    delays = [5, 10, 15, 25, 40, 60, 60, 60]
    requested_up_model = kwargs.get("model", TARGET_MODEL)
    routes = _get_dispatch_routes(requested_up_model)

    for attempt in range(max_retries + 1):
        route_idx = min(attempt, len(routes) - 1)
        acc_email, acc_proj, target_client, route_model = routes[route_idx]
        call_kwargs = dict(kwargs, model=route_model)
        func = getattr(target_client.aio.models, method_name)
        try:
            res = await func(*args, **call_kwargs)
            return res, acc_email, acc_proj, route_model
        except Exception as e:
            if is_retryable_error(e) and attempt < max_retries:
                if attempt + 1 < len(routes):
                    next_email, next_proj, _, next_model = routes[attempt + 1]
                    print(
                        f"[POOL_FAILOVER] {acc_email} ({route_model}) hit 429/transient error. "
                        f"Instantly switching to {next_email} ({next_proj} / {next_model})...",
                        flush=True,
                    )
                    continue
                await reset_client(acc_email)
                routes = _get_dispatch_routes(requested_up_model)
                wait_sec = delays[min(attempt, len(delays) - 1)]
                print(
                    f"[RATE_LIMIT_GUARD] Transient error on {acc_email or acc_proj} ({route_model}): {e}. "
                    f"Reset connection pool and backoff {wait_sec}s (retry {attempt + 1}/{max_retries})...",
                    flush=True,
                )
                await asyncio.sleep(wait_sec)
            else:
                raise


def sanitize_chunk(obj):
    """
    核心兼容层：清洗响应数据，确保符合 Go 语言客户端 (agy) 的反序列化要求。
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

    return auto_compact_contents(cleaned)


def _compact_string_payload(val, max_len=800):
    if isinstance(val, str) and len(val) > max_len:
        return (
            val[:max_len]
            + f"\n... [BRIDGE_AUTO_COMPACT: truncated {len(val) - max_len} chars from old turn to save tokens & prevent 429] ..."
        )
    elif isinstance(val, dict):
        return {k: _compact_string_payload(v, max_len) for k, v in val.items()}
    elif isinstance(val, list):
        if len(val) > 20:
            return [_compact_string_payload(x, max_len) for x in val[:20]] + [
                f"... [{len(val) - 20} items omitted by bridge auto-compact]"
            ]
        return [_compact_string_payload(x, max_len) for x in val]
    return val


def auto_compact_contents(turns: list) -> list:
    """
    桥接层无损协议 Auto-Compact（滑动窗口自动瘦身）：
    - 当总序列化字符数超过 AUTO_COMPACT_CHAR_LIMIT（默认 180,000 字符，约 5w~6w tokens）且轮数较多时自动触发。
    - 永远完整保留开头前 2 轮（用户的初始任务目标与上下文摘要）和最近 AUTO_COMPACT_RECENT_TURNS 轮（默认最近 12 轮）。
    - 对中间陈旧历史轮次中的超长 functionResponse（如大文件读取、长终端输出）及超长文本进行智能折叠，
      100% 保持 functionCall 与 functionResponse 的配对结构和角色顺序，彻底杜绝 30w Token 膨胀导致的 429 与高额计费。
    """
    if not turns or len(turns) <= AUTO_COMPACT_RECENT_TURNS + 2:
        return turns
    try:
        raw_size = len(json.dumps(turns, ensure_ascii=False))
    except Exception:
        return turns

    if raw_size <= AUTO_COMPACT_CHAR_LIMIT:
        return turns

    cutoff = len(turns) - AUTO_COMPACT_RECENT_TURNS
    compacted_turns = []
    for idx, turn in enumerate(turns):
        if idx < 2 or idx >= cutoff:
            compacted_turns.append(turn)
            continue

        new_parts = []
        for p in turn.get("parts", []):
            if not isinstance(p, dict):
                new_parts.append(p)
                continue
            if "functionResponse" in p and isinstance(p["functionResponse"], dict):
                fr = dict(p["functionResponse"])
                fr["response"] = _compact_string_payload(fr.get("response", {}), max_len=600)
                new_parts.append({**p, "functionResponse": fr})
            elif "functionCall" in p and isinstance(p["functionCall"], dict):
                fc = dict(p["functionCall"])
                fc["args"] = _compact_string_payload(fc.get("args", {}), max_len=600)
                new_parts.append({**p, "functionCall": fc})
            elif "text" in p and isinstance(p["text"], str) and len(p["text"]) > 1000:
                if p.get("thought"):
                    # 陈旧轮次的思考文本但若带有 thoughtSignature 则保留签名，仅缩短文本
                    new_parts.append({**p, "text": p["text"][:300] + " ... [old thought compacted]"})
                else:
                    new_parts.append({**p, "text": _compact_string_payload(p["text"], max_len=1000)})
            else:
                new_parts.append(p)
        compacted_turns.append({"role": turn["role"], "parts": new_parts})

    try:
        new_size = len(json.dumps(compacted_turns, ensure_ascii=False))
        if new_size < raw_size:
            print(
                f"[BRIDGE_AUTO_COMPACT] Compacted middle turns (2..{cutoff - 1}): "
                f"{raw_size:,} chars -> {new_size:,} chars (saved {(1 - new_size / raw_size) * 100:.1f}%)",
                flush=True,
            )
    except Exception:
        pass
    return compacted_turns


@app.get("/healthz")
async def healthz():
    active_proj = (
        pool_manager._get_account_dict(pool_manager.active_email)["project_id"]
        if pool_manager.active_email and pool_manager._get_account_dict(pool_manager.active_email)
        else PROJECT_ID
    )
    return {
        "status": "ready",
        "bridge": "agy-argon-bridge",
        "version": VERSION,
        "model": TARGET_MODEL,
        "project": active_proj,
        "location": LOCATION,
        "account_pool": pool_manager.summary(),
        "last_response": last_response,
    }


@app.get("/accounts")
async def get_accounts():
    """查看多账号池实时用量、剩余安全额度与轮转状态"""
    return pool_manager.summary()


@app.get("/v1beta/models")
@app.get("/v1/models")
async def list_models():
    """提供与 Gemini API 兼容的模型列表，让 agy 识别模型可用性"""
    return {
        "models": [
            {
                "name": "models/gemini-4-carbon",
                "displayName": "Gemini 4 Carbon",
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent", "countTokens"],
            },
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
            },
        ]
    }


def build_config(body: dict, model: str = TARGET_MODEL) -> types.GenerateContentConfig:
    config_args = dict(body.get("generationConfig") or body.get("generation_config") or {})

    # 1. 保留客户端注入的系统角色与工作流指令
    sys_inst = body.get("systemInstruction") or body.get("system_instruction")
    if sys_inst:
        config_args["system_instruction"] = sys_inst

    # 2. 透传 agy 客户端声明的所有本地工具
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
    if model in (TARGET_MODEL, "gemini-4-argon", "gemini-4-carbon"):
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


def response_dict(response, requested_model, upstream_model, account_email=None, account_project=None):
    global last_response
    data = response.model_dump(by_alias=True, mode="json", exclude_none=True)
    data.pop("sdkHttpResponse", None)
    if data.get("modelVersion"):
        usage = data.get("usageMetadata")
        resp_id = data.get("responseId")
        cost_usd = pool_manager.record_usage(account_email, resp_id, usage) if account_email else 0.0

        last_response = {
            "at": datetime.now(timezone.utc).isoformat(),
            "account": account_email,
            "project": account_project or PROJECT_ID,
            "requested_model": requested_model,
            "upstream_model": upstream_model,
            "modelVersion": data["modelVersion"],
            "responseId": resp_id,
        }
        if usage:
            last_response["usageMetadata"] = usage
            if cost_usd > 0:
                last_response["estimatedCostUsd"] = round(cost_usd, 6)
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
    # 图片模型保留原模型，其余 Gemini 文本请求映射到 TARGET_MODEL。
    upstream_model = requested_model if "image" in requested_model else TARGET_MODEL
    body = await request.json()
    raw_contents = body.get("contents", [])

    # 核心净化：剔除死循环噪音与非法混杂轮次，确保符合 Vertex AI 规范
    clean_contents = sanitize_conversation_history(raw_contents)

    parsed_contents = []
    for c in clean_contents:
        try:
            parsed_contents.append(types.Content.model_validate(c))
        except Exception:
            parsed_contents.append(c)

    targets = _get_dispatch_targets()
    primary_email, primary_proj, _ = targets[0]
    has_tools = bool(body.get("tools"))
    print(f"\n==================== [AGY REQUEST] ====================")
    print(f"Path: {model_path}")
    print(f"Active Account: {primary_email or 'default'} (Project: {primary_proj})")
    print(f"Has Local Tools: {has_tools} (Declarations count: {len(body.get('tools', []))})")
    print(f"Contents count: raw={len(raw_contents)} -> sanitized={len(parsed_contents)}")
    print(f"Routing -> Google Cloud Vertex AI ({upstream_model})", flush=True)
    print(f"=======================================================\n")

    # 1. 处理 Token 计算
    if operation == "countTokens":
        try:
            res, _, _, _ = await execute_with_pool_backoff(
                "count_tokens",
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

    # 2. 处理流式输出（首帧 240s 超时看门狗、跨账号 + 跨模型 Carbon/Argon 429 自动分流、12 分钟长退避自愈）
    if is_stream:
        max_retries = 12
        delays = [8, 12, 18, 25, 35, 45, 60, 60, 60, 60, 60, 60]
        response_stream = None
        first_chunk = None
        used_email = primary_email
        used_proj = primary_proj
        used_model = upstream_model

        for attempt in range(max_retries + 1):
            routes = _get_dispatch_routes(upstream_model)
            route_idx = attempt % len(routes) if attempt < len(routes) * 2 else min(attempt, len(routes) - 1)
            used_email, used_proj, target_client, used_model = routes[route_idx]
            try:
                response_stream = await target_client.aio.models.generate_content_stream(
                    model=used_model,
                    contents=parsed_contents,
                    config=cfg,
                )
                first_chunk = await asyncio.wait_for(anext(response_stream), timeout=240.0)
                break
            except StopAsyncIteration:
                break
            except Exception as e:
                if is_retryable_error(e) and attempt < max_retries:
                    if attempt + 1 < len(routes):
                        next_email, next_proj, _, next_model = routes[attempt + 1]
                        print(
                            f"\n[POOL_FAILOVER] Upstream {used_model} on {used_email} hit 429/transient error. "
                            f"Instantly switching request to {next_email} ({next_proj} / model={next_model})...",
                            flush=True,
                        )
                        continue
                    await reset_client(used_email)
                    wait_sec = delays[min(attempt - len(routes) + 1, len(delays) - 1)]
                    print(
                        f"\n[RATE_LIMIT_GUARD] Upstream {used_model} on {used_email} hit rate limit / temporary error: {type(e).__name__} ({e})",
                        flush=True,
                    )
                    print(
                        f"[RATE_LIMIT_GUARD] Reset HTTP/2 client connection pool. Entering silent backoff: waiting {wait_sec}s (retry {attempt + 1}/{max_retries})...",
                        flush=True,
                    )
                    await asyncio.sleep(wait_sec)
                else:
                    print(f"Streaming error before first chunk on {used_model}: {e}", file=sys.stderr, flush=True)
                    return upstream_error(e)

        async def event_generator():
            if response_stream is None:
                return
            has_emitted_content = False
            try:
                if first_chunk is not None:
                    first_dict = response_dict(
                        first_chunk, requested_model, used_model, used_email, used_proj
                    )
                    for cand in first_dict.get("candidates", []):
                        for p in cand.get("content", {}).get("parts", []):
                            if (p.get("text") and not p.get("thought")) or "functionCall" in p:
                                has_emitted_content = True
                    yield f"data: {json.dumps(first_dict)}\n\n"

                async for chunk in response_stream:
                    chunk_dict = response_dict(
                        chunk, requested_model, used_model, used_email, used_proj
                    )
                    for cand in chunk_dict.get("candidates", []):
                        for p in cand.get("content", {}).get("parts", []):
                            if (p.get("text") and not p.get("thought")) or "functionCall" in p:
                                has_emitted_content = True
                    yield f"data: {json.dumps(chunk_dict)}\n\n"
            except Exception as e:
                print(f"Streaming error during stream iteration on {used_model}: {e}", file=sys.stderr, flush=True)
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
            resp, used_email, used_proj, used_model = await execute_with_pool_backoff(
                "generate_content",
                model=upstream_model,
                contents=parsed_contents,
                config=cfg,
            )
            resp_dict = response_dict(resp, requested_model, used_model, used_email, used_proj)
            return JSONResponse(content=resp_dict)
        except Exception as e:
            print(f"GenerateContent error on {upstream_model}: {e}", file=sys.stderr)
            return upstream_error(e)


if __name__ == "__main__":
    port = int(os.environ.get("ARGON_BRIDGE_PORT", 8050))
    print(f"Starting agy-argon-bridge v{VERSION} on 127.0.0.1:{port} (Active: {pool_manager.active_email}, Project: {PROJECT_ID})...")
    uvicorn.run(app, host="127.0.0.1", port=port, timeout_keep_alive=600)
