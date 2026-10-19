import asyncio
import base64
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from google.genai import types

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "audit-fixture-project")
spec = importlib.util.spec_from_file_location("bridge", Path(__file__).parents[1] / "gemini_argon_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

BODY = {"contents": [{"role": "user", "parts": [{"text": "fixture"}]}]}

class FakeModels:
    def __init__(self):
        self.calls = []
        self.delay = 0
        self.stream_error = None

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(self.delay)
        return types.GenerateContentResponse(model_version=kwargs["model"], response_id="fixture-response", candidates=[
            types.Candidate(content=types.Content(role="model", parts=[types.Part(text="fixture-ok")]))])

    async def generate_content_stream(self, **kwargs):
        async def stream():
            if self.stream_error:
                raise self.stream_error
            yield await self.generate_content(**kwargs)
        return stream()

    async def count_tokens(self, **kwargs):
        self.calls.append(kwargs)
        return types.CountTokensResponse(total_tokens=42)

class ConfigTests(unittest.TestCase):
    def test_reasoning_and_structured_output_survive(self):
        cfg = bridge.build_config({"generationConfig": {
            "thinkingConfig": {"thinkingLevel": "HIGH"}, "responseMimeType": "application/json",
            "responseSchema": {"type": "OBJECT", "properties": {"answer": {"type": "STRING"}}},
            "maxOutputTokens": 256,
        }})
        self.assertEqual(cfg.thinking_config.thinking_level, "HIGH")
        self.assertEqual(cfg.response_mime_type, "application/json")
        self.assertIsNotNone(cfg.response_schema)
        self.assertEqual(cfg.max_output_tokens, 256)

    def test_legacy_reasoning_budget_does_not_break_argon(self):
        cfg = bridge.build_config({"generationConfig": {"thinkingConfig": {"thinkingBudget": 16384}}})
        self.assertIsNone(cfg.thinking_config.thinking_budget)
        cfg = bridge.build_config({"generationConfig": {"thinkingConfig": {"thinkingLevel": "MINIMAL"}}})
        self.assertEqual(cfg.thinking_config.thinking_level, "LOW")

    def test_agy_effort_budgets_preserve_reasoning_levels(self):
        # 实际 AGY 1.3.5 请求：LOW=1000、MEDIUM=4000、HIGH=-1。
        for budget, level in [(1000, "LOW"), (4000, "MEDIUM"), (-1, "HIGH")]:
            with self.subTest(budget=budget):
                cfg = bridge.build_config({"generationConfig": {"thinkingConfig": {"thinkingBudget": budget}}})
                self.assertEqual(cfg.thinking_config.thinking_level, level)

    def test_explicit_reasoning_level_wins_over_legacy_budget(self):
        cfg = bridge.build_config({"generationConfig": {"thinkingConfig": {"thinkingBudget": 1000, "thinkingLevel": "HIGH"}}})
        self.assertEqual(cfg.thinking_config.thinking_level, "HIGH")

    def test_signature_roundtrip_preserves_bytes(self):
        raw = b"\xfb\xef\xff\x00\x01"
        cleaned = bridge.sanitize_chunk({"thoughtSignature": base64.urlsafe_b64encode(raw).decode().rstrip("=")})
        part = types.Part.model_validate(cleaned)
        self.assertEqual(part.thought_signature, raw)

    def test_inline_media_uses_standard_base64_without_changing_tool_data(self):
        raw = b"\xfb\xef\xff\x00\x01"
        response = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[
            types.Part(inline_data=types.Blob(data=raw, mime_type="image/png")),
            types.Part(function_call=types.FunctionCall(name="fixture", args={"data": "keep-me_-"})),
        ]))])
        for data in [bridge.response_dict(response, "fixture-image", "fixture-image"),
                     bridge.sanitize_chunk({"candidates": [{"content": {"parts": [{"inlineData": {
                         "data": base64.urlsafe_b64encode(raw).decode().rstrip("="), "mimeType": "image/png"
                     }}]}}]})]:
            encoded = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
            self.assertEqual(base64.b64decode(encoded, validate=True), raw)
        self.assertEqual(bridge.response_dict(response, "fixture-image", "fixture-image")
                         ["candidates"][0]["content"]["parts"][1]["functionCall"]["args"]["data"], "keep-me_-")

    def test_sanitize_isolates_function_call_and_response(self):
        # 1. 模拟工具调用与响应混杂在同一轮次中（Vertex AI 400 核心诱因）
        raw_mixed = [
            {"role": "user", "parts": [{"text": "query"}]},
            {"role": "model", "parts": [
                {"functionCall": {"name": "view_file", "args": {"path": "a.py"}}},
                {"functionResponse": {"name": "view_file", "response": {"content": "code"}}}
            ]}
        ]
        sanitized = bridge.sanitize_conversation_history(raw_mixed)
        for turn in sanitized:
            has_fc = any("functionCall" in p for p in turn["parts"])
            has_fr = any("functionResponse" in p for p in turn["parts"])
            self.assertFalse(has_fc and has_fr, f"Turn cannot mix functionCall and functionResponse: {turn}")

        # 2. 模拟连续相同 role 被打错导致的混淆合并
        raw_consecutive = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "test"}}]},
            {"role": "model", "parts": [{"functionResponse": {"name": "test"}}]}
        ]
        sanitized2 = bridge.sanitize_conversation_history(raw_consecutive)
        for turn in sanitized2:
            has_fc = any("functionCall" in p for p in turn["parts"])
            has_fr = any("functionResponse" in p for p in turn["parts"])
            self.assertFalse(has_fc and has_fr, f"Consecutive turns merged illegal parts: {turn}")
        self.assertEqual(sanitized2[1]["role"], "model")
        self.assertEqual(sanitized2[2]["role"], "user")

    def test_no_author_project_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bridge.subprocess, "run", side_effect=FileNotFoundError), \
             patch.object(bridge.google.auth, "default", return_value=(SimpleNamespace(quota_project_id=None), None)):
            with self.assertRaisesRegex(RuntimeError, "GOOGLE_CLOUD_PROJECT"):
                bridge.get_default_project_id()

class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.models = FakeModels()
        self.client_patch = patch.object(bridge, "client", SimpleNamespace(aio=SimpleNamespace(models=self.models)))
        self.client_patch.start()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app), base_url="http://fixture")

    async def asyncTearDown(self):
        await self.http.aclose()
        self.client_patch.stop()

    async def test_health_stays_responsive_during_generation(self):
        self.models.delay = 0.3
        request = asyncio.create_task(self.http.post("/v1beta/models/gemini-3.8-flash:generateContent", json=BODY))
        await asyncio.sleep(0.02)
        health = await asyncio.wait_for(self.http.get("/healthz"), timeout=0.1)
        self.assertEqual(health.status_code, 200)
        self.assertFalse(request.done())
        self.assertEqual((await request).json()["modelVersion"], "gemini-4-argon")

    async def test_image_model_and_modalities_are_preserved(self):
        body = {**BODY, "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
        response = await self.http.post("/v1beta/models/gemini-3.1-flash-image:generateContent", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.models.calls[-1]["model"], "gemini-3.1-flash-image")
        self.assertEqual(self.models.calls[-1]["config"].response_modalities, ["TEXT", "IMAGE"])

    async def test_count_tokens_includes_system_and_tools(self):
        body = {**BODY, "systemInstruction": {"parts": [{"text": "system fixture"}]},
                "tools": [{"functionDeclarations": [{"name": "read_fixture"}]}]}
        response = await self.http.post("/v1beta/models/gemini-3.8-flash:countTokens", json=body)
        self.assertEqual(response.json()["totalTokens"], 42)
        self.assertIsNotNone(self.models.calls[-1]["config"].system_instruction)
        self.assertIsNotNone(self.models.calls[-1]["config"].tools)

    async def test_upstream_failure_before_stream_is_http_error(self):
        error = RuntimeError("fixture forbidden")
        error.code = 403
        self.models.stream_error = error
        response = await self.http.post("/v1beta/models/gemini-3.8-flash:streamGenerateContent", json=BODY)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], 403)

    async def test_sse_contains_real_upstream_version(self):
        response = await self.http.post("/v1beta/models/gemini-3.8-flash:streamGenerateContent", json=BODY)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"modelVersion": "gemini-4-argon"', response.text)
        health = (await self.http.get("/healthz")).json()
        self.assertEqual(health["last_response"]["responseId"], "fixture-response")

    async def test_unknown_operation_rejected(self):
        response = await self.http.post("/v1beta/models/gemini-4-argon:notAnOperation", json=BODY)
        self.assertEqual(response.status_code, 404)

if __name__ == "__main__":
    unittest.main()
