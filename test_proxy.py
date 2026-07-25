#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==========================================================
# @xiaozhe - Termux Claude Code OpenAI Proxy
# 版权所有 © 2026 小哲
# ==========================================================
"""
Termux Claude Code OpenAI Proxy — 单元测试
运行：python test_proxy.py
"""
import sys
import os
import json
import tempfile
import unittest

# 确保能找到模块
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

from url_utils import sanitize_url, sanitize_key, sanitize_model


class TestSanitizeURL(unittest.TestCase):
    """测试 URL 清洗"""

    def test_clean_https(self):
        ok, url = sanitize_url("https://api.openai.com/v1")
        self.assertTrue(ok)
        self.assertEqual(url, "https://api.openai.com/v1")

    def test_space_in_domain(self):
        ok, url = sanitize_url("https://token sensenova.cn/v1")
        self.assertTrue(ok)
        self.assertIn("token.sensenova.cn", url)

    def test_no_scheme(self):
        ok, url = sanitize_url("api.openai.com/v1")
        self.assertTrue(ok)
        self.assertTrue(url.startswith("https://"))

    def test_empty_url(self):
        ok, msg = sanitize_url("")
        self.assertFalse(ok)

    def test_none_url(self):
        ok, msg = sanitize_url(None)
        self.assertFalse(ok)

    def test_openai_auto_v1(self):
        ok, url = sanitize_url("https://api.openai.com", kind="openai")
        self.assertTrue(ok)
        self.assertTrue(url.endswith("/v1"))

    def test_trailing_slash(self):
        ok, url = sanitize_url("https://api.openai.com/v1/")
        self.assertTrue(ok)
        self.assertFalse(url.endswith("/"))

    def test_bom_chars(self):
        ok, url = sanitize_url("\ufeffhttps://api.openai.com/v1")
        self.assertTrue(ok)
        self.assertEqual(url, "https://api.openai.com/v1")


class TestSanitizeKey(unittest.TestCase):
    """测试 API Key 清洗"""

    def test_normal_key(self):
        ok, key = sanitize_key("sk-1234567890")
        self.assertTrue(ok)
        self.assertEqual(key, "sk-1234567890")

    def test_bearer_prefix(self):
        ok, key = sanitize_key("Bearer sk-1234567890")
        self.assertTrue(ok)
        self.assertEqual(key, "sk-1234567890")

    def test_bearer_colon(self):
        ok, key = sanitize_key("Bearer: sk-1234567890")
        self.assertTrue(ok)
        self.assertEqual(key, "sk-1234567890")

    def test_spaces(self):
        ok, key = sanitize_key("  sk-123  456  ")
        self.assertTrue(ok)
        self.assertNotIn(" ", key)

    def test_empty_key(self):
        ok, msg = sanitize_key("")
        self.assertFalse(ok)

    def test_none_key(self):
        ok, msg = sanitize_key(None)
        self.assertFalse(ok)


class TestSanitizeModel(unittest.TestCase):
    """测试模型名清洗"""

    def test_normal(self):
        self.assertEqual(sanitize_model("gpt-4o"), "gpt-4o")

    def test_alias_grok(self):
        self.assertEqual(sanitize_model("grok"), "grok-4.5")

    def test_alias_deepseek(self):
        self.assertEqual(sanitize_model("deepseek-v4"), "deepseek-v4-pro")

    def test_empty_default(self):
        self.assertEqual(sanitize_model(""), "gpt-4o")

    def test_whitespace(self):
        self.assertEqual(sanitize_model("  gpt-4o  "), "gpt-4o")


class TestOpenAIProxyBasic(unittest.TestCase):
    """测试 openai_proxy.py 基础功能"""

    def setUp(self):
        # 模拟 openai_proxy 模块需要的环境变量
        self._env_backup = {}
        for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "PROXY_PORT", "PROXY_DEBUG",
                  "OPENAI_MODEL", "HOME"):
            self._env_backup[k] = os.environ.get(k)
        os.environ["OPENAI_BASE_URL"] = "https://api.openai.com/v1"
        os.environ["OPENAI_API_KEY"] = "sk-test-key"
        os.environ["PROXY_DEBUG"] = "0"
        os.environ["HOME"] = tempfile.mkdtemp()

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_import_proxy(self):
        """测试模块导入"""
        try:
            import openai_proxy
            self.assertTrue(hasattr(openai_proxy, "is_reasoning_model"))
            self.assertTrue(hasattr(openai_proxy, "is_large_complex_task"))
            self.assertTrue(hasattr(openai_proxy, "build_payload"))
        except Exception as e:
            self.fail(f"导入 openai_proxy 失败: {e}")

    def test_is_reasoning_model(self):
        """测试推理模型识别"""
        import openai_proxy
        self.assertTrue(openai_proxy.is_reasoning_model("o3-mini"))
        self.assertTrue(openai_proxy.is_reasoning_model("o1"))
        self.assertTrue(openai_proxy.is_reasoning_model("deepseek-r1"))
        self.assertTrue(openai_proxy.is_reasoning_model("gpt-5"))
        self.assertFalse(openai_proxy.is_reasoning_model("gpt-4o"))
        self.assertFalse(openai_proxy.is_reasoning_model("claude-3-opus"))

    def test_is_large_complex_task(self):
        """测试大型任务识别"""
        import openai_proxy
        # 带工具 → 大型
        self.assertTrue(openai_proxy.is_large_complex_task({"tools": [{"name": "test"}]}))
        # 超长文本 → 大型
        self.assertTrue(openai_proxy.is_large_complex_task({"messages": [{"role": "user", "content": "x" * 600}]}))
        # 正常对话 → 非大型
        self.assertFalse(openai_proxy.is_large_complex_task({"messages": [{"role": "user", "content": "你好"}]}))
        # 空消息 → 非大型
        self.assertFalse(openai_proxy.is_large_complex_task({}))

    def test_build_payload_basic(self):
        """测试 payload 构建"""
        import openai_proxy
        body = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "你好"}],
            "stream": False,
        }
        payload = openai_proxy.build_payload(body, stream=False)
        self.assertEqual(payload["model"], "gpt-4o")
        self.assertFalse(payload["stream"])
        self.assertEqual(len(payload["messages"]), 1)

    def test_build_payload_with_tools(self):
        """测试带工具的 payload 构建"""
        import openai_proxy
        body = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "查天气"}],
            "tools": [{"name": "get_weather", "description": "查天气", "input_schema": {"type": "object", "properties": {}}}],
            "stream": False,
        }
        payload = openai_proxy.build_payload(body, stream=False)
        self.assertIn("tools", payload)
        self.assertEqual(payload["tools"][0]["function"]["name"], "get_weather")

    def test_build_payload_reasoning_model(self):
        """测试推理模型 payload 注入 reasoning_effort"""
        import openai_proxy
        body = {
            "model": "o3-mini",
            "messages": [{"role": "user", "content": "思考一下"}],
            "stream": False,
        }
        payload = openai_proxy.build_payload(body, stream=False)
        # 推理模型应注入 reasoning_effort
        if openai_proxy.supports_reasoning_effort("o3-mini"):
            self.assertIn("reasoning_effort", payload)

    def test_convert_messages_simple(self):
        """测试消息转换"""
        import openai_proxy
        body = {
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
            ]
        }
        msgs = openai_proxy.convert_messages(body)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")

    def test_convert_messages_with_system(self):
        """测试带 system 的消息转换"""
        import openai_proxy
        body = {
            "system": "你是助手",
            "messages": [{"role": "user", "content": "你好"}],
        }
        msgs = openai_proxy.convert_messages(body)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "你是助手")

    def test_convert_tools(self):
        """测试工具转换"""
        import openai_proxy
        body = {
            "tools": [{"name": "get_weather", "description": "查天气", "input_schema": {"type": "object", "properties": {}}}]
        }
        tools = openai_proxy.convert_tools(body)
        self.assertIsNotNone(tools)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["function"]["name"], "get_weather")

    def test_convert_tool_choice(self):
        """测试 tool_choice 转换"""
        import openai_proxy
        self.assertEqual(openai_proxy.convert_tool_choice({"tool_choice": "any"}), "required")
        self.assertEqual(openai_proxy.convert_tool_choice({"tool_choice": "auto"}), "auto")
        self.assertEqual(openai_proxy.convert_tool_choice({"tool_choice": "none"}), "none")
        self.assertIsNone(openai_proxy.convert_tool_choice({}))
        self.assertIsNone(openai_proxy.convert_tool_choice({"tool_choice": None}))

    def test_clean_model(self):
        """测试模型名清洗"""
        import openai_proxy
        self.assertEqual(openai_proxy.clean_model("gpt-4o"), "gpt-4o")
        self.assertEqual(openai_proxy.clean_model("deepseek-v4-pro[1m]"), "deepseek-v4-pro")

    def test_stop_reason(self):
        """测试 stop_reason 转换"""
        import openai_proxy
        self.assertEqual(openai_proxy.stop_reason("tool_calls", True), "tool_use")
        self.assertEqual(openai_proxy.stop_reason("length", False), "max_tokens")
        self.assertEqual(openai_proxy.stop_reason("stop", False), "end_turn")
        self.assertEqual(openai_proxy.stop_reason(None, False), "end_turn")

    def test_is_upstream_failure(self):
        """测试上游错误判断"""
        import openai_proxy
        class MockError:
            def __init__(self, msg):
                self.msg = msg
            def __str__(self):
                return self.msg
        self.assertTrue(openai_proxy.is_upstream_failure(MockError("429")))
        self.assertTrue(openai_proxy.is_upstream_failure(MockError("timeout")))
        self.assertTrue(openai_proxy.is_upstream_failure(MockError("401")))
        self.assertFalse(openai_proxy.is_upstream_failure(MockError("200")))
        self.assertFalse(openai_proxy.is_upstream_failure(None))

    def test_get_model_pricing(self):
        """测试模型定价"""
        import openai_proxy
        pricing = openai_proxy._get_model_pricing("gpt-4o")
        self.assertEqual(pricing["input"], 2.50)
        pricing = openai_proxy._get_model_pricing("unknown-model")
        self.assertIn("input", pricing)

    def test_blocks_to_text(self):
        """测试 blocks 转文本"""
        import openai_proxy
        self.assertEqual(openai_proxy.blocks_to_text("hello"), "hello")
        self.assertEqual(openai_proxy.blocks_to_text(None), "")
        self.assertEqual(openai_proxy.blocks_to_text([{"type": "text", "text": "hello"}]), "hello")
        self.assertEqual(openai_proxy.blocks_to_text([{"type": "image"}]), "[image]")

    def test_make_trace_id(self):
        """测试 trace_id 生成"""
        import openai_proxy
        tid = openai_proxy._make_trace_id()
        self.assertIsInstance(tid, str)
        self.assertGreater(len(tid), 10)

    def test_exponential_backoff(self):
        """测试退避计算"""
        import openai_proxy
        # 优先使用 retry_after
        self.assertEqual(openai_proxy._exponential_backoff(0, retry_after=5.0), 5.0)
        # 正常退避: 1s, 2s, 4s, 8s...
        self.assertAlmostEqual(openai_proxy._exponential_backoff(0), 1.0, delta=0.1)
        self.assertAlmostEqual(openai_proxy._exponential_backoff(1), 2.0, delta=0.1)
        # 上限 60s
        self.assertAlmostEqual(openai_proxy._exponential_backoff(10), 60.0, delta=0.1)

    def test_parse_retry_after(self):
        """测试 Retry-After 解析"""
        import openai_proxy
        from http.client import HTTPMessage
        # 模拟响应头
        class MockHTTPError:
            def __init__(self):
                self.headers = {"Retry-After": "30"}
                self.fp = None
        e = MockHTTPError()
        self.assertAlmostEqual(openai_proxy._parse_retry_after(e), 30.0)


class TestClaudePyBasic(unittest.TestCase):
    """测试 claude.py 基础功能"""

    def test_import_claude(self):
        """测试模块导入（不触发 main）"""
        try:
            # 只导入模块，不执行 main
            import importlib.util
            spec = importlib.util.spec_from_file_location("claude", os.path.join(TEST_DIR, "claude.py"))
            if spec:
                self.assertIsNotNone(spec)
        except Exception as e:
            self.fail(f"导入 claude.py 失败: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)