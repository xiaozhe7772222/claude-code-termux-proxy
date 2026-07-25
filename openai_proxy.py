#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==========================================================
# @xiaozhe - Termux Claude Code OpenAI Proxy
# 版权所有 © 2026 小哲
# ==========================================================
"""
Anthropic /v1/messages  ->  OpenAI 兼容 /v1/chat/completions
给 Claude Code 用的本地轻量代理（Termux 友好，仅标准库）
"""

import io
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from url_utils import sanitize_url

# ── Termux 环境常量 ──
_HOME = os.environ.get("HOME", "/data/data/com.termux/files/home")

def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()

_RAW_BASE = env("OPENAI_BASE_URL") or "https://api.openai.com/v1"
_ok_base, _base_or_err = sanitize_url(_RAW_BASE, kind="openai")
OPENAI_API_KEY = env("OPENAI_API_KEY") or env("PROXY_OPENAI_API_KEY")
OPENAI_BASE_URL = _base_or_err if _ok_base else ""
_BASE_URL_ERROR = "" if _ok_base else _base_or_err
DEFAULT_MODEL = env("OPENAI_MODEL") or env("ANTHROPIC_MODEL") or "gpt-4o"
PROXY_HOST = env("PROXY_HOST") or "127.0.0.1"
PROXY_PORT = int(env("PROXY_PORT") or "8765")
DEBUG = env("PROXY_DEBUG", "1") not in ("0", "false", "False")
FORCE_NON_STREAM = env("PROXY_FORCE_NON_STREAM", "1") not in ("0", "false", "False")
DROP_TOOLS = env("PROXY_DROP_TOOLS", "0") not in ("0", "false", "False")

# ── Token usage stats ──
STATS_FILE = os.path.join(_HOME, ".claude", "usage_stats.json")
_stats_lock = threading.Lock()

MODEL_PRICING = {
    "gpt-4o":          {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":     {"input": 0.15,  "output": 0.60},
    "deepseek":        {"input": 0.28,  "output": 1.10},
    "deepseek-v4":     {"input": 0.28,  "output": 1.10},
    "glm":             {"input": 0.50,  "output": 2.00},
    "doubao":          {"input": 0.80,  "output": 3.00},
    "mimo":            {"input": 0.60,  "output": 2.50},
    "grok":            {"input": 2.00,  "output": 10.00},
    "claude":          {"input": 3.00,  "output": 15.00},
    "default":         {"input": 1.00,  "output": 3.00},
}

# ── 推理增强策略（单模型）──
_LARGE_KEYWORDS = (
    "重构", "架构", "全面", "完整", "系统", "逆向", "审计", "深度", "详细分析",
    "多文件", "全量", "迁移", "性能优化", "安全", "漏洞", "设计方案", "实现计划",
    "refactor", "architecture", "audit", "security", "vulnerability", "migrate",
    "end-to-end", "thorough", "comprehensive", "multi-file", "implement", "debug all",
    "fix all", "analyze entire", "codebase", "整个项目", "整包", "批量",
)


def _user_text_blob(body: dict) -> str:
    """提取最近用户文本，用于判断是否大型问题"""
    parts = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if (msg.get("role") or "") != "user":
            continue
        c = msg.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") in ("text", "input_text"):
                    parts.append(str(b.get("text") or ""))
                elif isinstance(b, str):
                    parts.append(b)
    sys_ = body.get("system")
    if isinstance(sys_, str):
        parts.append(sys_)
    elif isinstance(sys_, list):
        for b in sys_:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
    return "\n".join(parts)


def is_large_complex_task(body: dict) -> bool:
    """判断是否为需要增强推理的大型任务。"""
    if not isinstance(body, dict):
        return False

    blob = _user_text_blob(body)
    blob_l = (blob or "").lower().strip()
    n_msgs = len(body.get("messages") or [])
    n_tools = len(body.get("tools") or []) if isinstance(body.get("tools"), list) else 0
    chars = len(blob or "")

    if n_tools > 0:
        return True
    if chars >= 500:
        return True
    if n_msgs >= 8:
        return True

    for kw in _LARGE_KEYWORDS:
        if kw.lower() in blob_l:
            return True

    if "```" in blob and chars >= 200:
        return True

    return False


# 高强度推理预算：可用环境变量覆盖。推理模型思维链会吃 token，默认给大一些
HIGH_REASONING_MAX_TOKENS = int(env("PROXY_HIGH_MAX_TOKENS") or "32000")
HIGH_REASONING_TEMPERATURE = float(env("PROXY_HIGH_TEMPERATURE") or "0.2")

# ── 采样/推理参数的「全局默认兜底」──
DEFAULT_TEMPERATURE = env("OPENAI_TEMPERATURE")
DEFAULT_TOP_P = env("OPENAI_TOP_P")
DEFAULT_MAX_TOKENS = env("OPENAI_MAX_TOKENS")
DEFAULT_FREQUENCY_PENALTY = env("OPENAI_FREQUENCY_PENALTY")
DEFAULT_PRESENCE_PENALTY = env("OPENAI_PRESENCE_PENALTY")
DEFAULT_SEED = env("OPENAI_SEED")
DEFAULT_REASONING_EFFORT = env("OPENAI_REASONING_EFFORT") or "high"
PARALLEL_TOOL_CALLS = env("OPENAI_PARALLEL_TOOL_CALLS", "1") not in ("0", "false", "False")

# 上游请求超时（推理模型很慢，60s 常被掐断）
UPSTREAM_TIMEOUT = int(env("PROXY_UPSTREAM_TIMEOUT") or "180")
# 手动声明哪些模型算“推理模型”（逗号分隔子串），补充自动识别
_EXTRA_REASONING = tuple(
    x.strip().lower() for x in (env("PROXY_REASONING_MODELS") or "").split(",") if x.strip()
)

# ── 新增：故障转移与重试配置 ──
_FAILOVER_PRESETS_FILE = os.path.join(_HOME, ".claude", "failover_presets.json")
_failover_lock = threading.Lock()
_failover_state = {
    "consecutive_failures": 0,
    "circuit_open_until": 0.0,
    "current_preset_index": 0,
    "last_failover_time": 0.0,
}

# 熔断阈值
_CIRCUIT_FAILURE_THRESHOLD = int(env("PROXY_CIRCUIT_THRESHOLD") or "5")
_CIRCUIT_COOLDOWN_SECONDS = int(env("PROXY_CIRCUIT_COOLDOWN") or "30")
# 重试次数
_MAX_RETRIES = int(env("PROXY_MAX_RETRIES") or "3")
# 退避基数（秒）
_BACKOFF_BASE = float(env("PROXY_BACKOFF_BASE") or "1.0")
def is_reasoning_model(model: str) -> bool:
    """识别推理类模型：它们只接受 temperature=1。"""
    m = (model or "").lower()
    if any(tag in m for tag in _EXTRA_REASONING):
        return True
    if re.match(r"^(o[1345])([:\-]|$)", m) or m.startswith("gpt-5") or m.startswith("gpt5"):
        return True
    return any(tag in m for tag in (
        "reasoner", "-reasoning", "-thinking", "-think", "qwq", "r1", "deepseek-r",
    ))


def supports_reasoning_effort(model: str) -> bool:
    """是否支持 reasoning_effort 参数（OpenAI o系列 / gpt-5）。"""
    m = (model or "").lower()
    return bool(re.match(r"^(o[1345])([:\-]|$)", m)) or m.startswith("gpt-5") or m.startswith("gpt5")

# 单模型推理增强（多Agent已移除）
HIGH_REASONING_SYSTEM = (
    "你是高强度推理助手。请深度思考，分步推理，验证结论。"
    "优先正确性与完整性；给出可执行细节。回答使用与用户相同的语言。"
    "【重要】禁止复述用户问题、禁止重复自己的前文、禁止在无新信息时循环分析。"
    "得出结论后立即停止，不要反复确认或自我怀疑。"
    "简洁优先——说清楚就停，不要啰嗦。"
)


def is_upstream_failure(err_obj) -> bool:
    """判断是否为上游 API 失败（限流/超时/服务器错误/鉴权问题）"""
    if err_obj is None:
        return False
    s = str(err_obj).lower()
    keys = (
        "429", "500", "502", "503", "504",
        "rate limit", "timeout", "timed out", "overloaded",
        "capacity", "unavailable", "connection", "reset",
        "invalid api key", "unauthorized", "401", "403",
        "model_not_found", "does not exist", "no access to model",
        "context_length", "maximum context", "too many tokens",
    )
    return any(k in s for k in keys)


def _get_model_pricing(model: str) -> dict:
    if not model:
        return MODEL_PRICING["default"]
    ml = model.lower()
    if ml in MODEL_PRICING:
        return MODEL_PRICING[ml]
    base = ml.split("-")[0].split("/")[0]
    if base in MODEL_PRICING:
        return MODEL_PRICING[base]
    for key in MODEL_PRICING:
        if key in base or base in key:
            return MODEL_PRICING[key]
    return MODEL_PRICING["default"]

def _record_usage(model: str, prompt_tokens: int, completion_tokens: int):
    """Record token usage to stats file (thread-safe)"""
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return
    with _stats_lock:
        try:
            stats = {}
            if os.path.exists(STATS_FILE):
                with open(STATS_FILE, "r", encoding="utf-8") as f:
                    stats = json.load(f)
            if not isinstance(stats, dict):
                stats = {}

            today = datetime.now().strftime("%Y-%m-%d")
            pricing = _get_model_pricing(model)
            cost = (prompt_tokens / 1_000_000 * pricing["input"] +
                    completion_tokens / 1_000_000 * pricing["output"])

            total = stats.setdefault("total", {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0})
            total["prompt_tokens"] += prompt_tokens
            total["completion_tokens"] += completion_tokens
            total["cost_usd"] = round(total["cost_usd"] + cost, 6)

            models = stats.setdefault("models", {})
            m_stats = models.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "last_used": ""})
            m_stats["prompt_tokens"] += prompt_tokens
            m_stats["completion_tokens"] += completion_tokens
            m_stats["cost_usd"] = round(m_stats["cost_usd"] + cost, 6)
            m_stats["last_used"] = datetime.now().strftime("%Y-%m-%d %H:%M")

            daily = stats.setdefault("daily", {})
            d_stats = daily.setdefault(today, {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "count": 0})
            d_stats["prompt_tokens"] += prompt_tokens
            d_stats["completion_tokens"] += completion_tokens
            d_stats["cost_usd"] = round(d_stats["cost_usd"] + cost, 6)
            d_stats["count"] += 1

            tmp_path = STATS_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2, ensure_ascii=False)
            try:
                os.chmod(tmp_path, 0o600)
            except Exception:
                pass
            os.replace(tmp_path, STATS_FILE)
        except Exception as e:
            log(f"stats save error: {e}", "ERROR")


def _make_trace_id() -> str:
    """生成请求追踪 ID"""
    return datetime.now().strftime("%Y%m%d%H%M%S") + "-" + hex(int(time.time() * 1000000) % 0xFFFFFFFF)[2:]


def log(msg: str, level: str = "INFO", trace_id: str = "") -> None:
    t = datetime.now().strftime("%m-%d %H:%M:%S")
    prefix = f"{t} [{level}]"
    if trace_id:
        prefix += f" [{trace_id}]"
    sys.stderr.write(f"{prefix} {msg}\n")
    sys.stderr.flush()


# ── 新增：故障转移预设管理 ──

def load_failover_presets() -> List[Dict[str, Any]]:
    """读取故障转移预设列表"""
    try:
        with open(_FAILOVER_PRESETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_failover_presets(presets: List[Dict[str, Any]]) -> None:
    """保存故障转移预设列表"""
    try:
        os.makedirs(os.path.dirname(_FAILOVER_PRESETS_FILE), exist_ok=True)
        tmp = _FAILOVER_PRESETS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(presets, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _FAILOVER_PRESETS_FILE)
    except Exception as e:
        log(f"save failover presets error: {e}", "ERROR")


def _next_available_preset(start_index: int = 0) -> Optional[Dict[str, Any]]:
    """从预设列表中获取下一个可用的预设（跳过熔断中的）"""
    presets = load_failover_presets()
    if not presets:
        return None
    now = time.time()
    for i in range(len(presets)):
        idx = (start_index + i) % len(presets)
        p = presets[idx]
        # 检查熔断状态
        circuit_until = p.get("circuit_open_until", 0)
        if circuit_until > now:
            continue
        return p
    return None


def _mark_preset_failure(preset_id: str, is_circuit: bool = False) -> None:
    """标记预设失败，触发熔断"""
    with _failover_lock:
        presets = load_failover_presets()
        for p in presets:
            if p.get("id") == preset_id:
                if is_circuit:
                    p["circuit_open_until"] = time.time() + _CIRCUIT_COOLDOWN_SECONDS
                    p["consecutive_failures"] = int(p.get("consecutive_failures", 0)) + 1
                else:
                    p["consecutive_failures"] = int(p.get("consecutive_failures", 0)) + 1
                    if p.get("consecutive_failures", 0) >= _CIRCUIT_FAILURE_THRESHOLD:
                        p["circuit_open_until"] = time.time() + _CIRCUIT_COOLDOWN_SECONDS
                break
        save_failover_presets(presets)


def _reset_preset_success(preset_id: str) -> None:
    """预设请求成功，重置失败计数"""
    with _failover_lock:
        presets = load_failover_presets()
        for p in presets:
            if p.get("id") == preset_id:
                p["consecutive_failures"] = 0
                p["circuit_open_until"] = 0
                p["last_success"] = time.time()
                break
        save_failover_presets(presets)


# ── 新增：连接池（简化版）──

class _ConnectionPool:
    """HTTP 连接池，复用上游连接"""
    def __init__(self):
        self._pool: Dict[str, urllib.request.HTTPHandler] = {}
        self._lock = threading.Lock()

    def get_handler(self, base_url: str) -> urllib.request.HTTPHandler:
        key = base_url.rstrip("/")
        with self._lock:
            if key not in self._pool:
                self._pool[key] = urllib.request.HTTPHandler()
            return self._pool[key]

    def invalidate(self, base_url: str) -> None:
        key = base_url.rstrip("/")
        with self._lock:
            self._pool.pop(key, None)

_conn_pool = _ConnectionPool()


# ── HTTP 请求（增强）──

def _build_opener(base_url: str):
    """构建带连接复用的 opener"""
    handler = _conn_pool.get_handler(base_url)
    return urllib.request.build_opener(handler)


def http_json(url: str, payload: Dict[str, Any], timeout: int = 60, trace_id: str = "") -> urllib.response.addinfourl:
    """发送 HTTP POST JSON 请求，带连接池和超时控制"""
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "User-Agent": "claude-code-openai-proxy/2.1",
        },
    )
    if DEBUG:
        log(f"POST {url} model={payload.get('model')} stream={payload.get('stream')} timeout={timeout}", trace_id=trace_id)
    opener = _build_opener(url.rsplit("/", 2)[0] + "//" + url.split("/")[2])
    return opener.open(req, timeout=timeout)


def _parse_retry_after(e: urllib.error.HTTPError) -> float:
    """从 429 响应头解析 Retry-After，返回秒数"""
    retry_after = e.headers.get("Retry-After") if e.headers else None
    if retry_after is None and e.fp:
        try:
            body = e.fp.read().decode("utf-8", errors="ignore")
            # 某些 API 在 body 里返回 retry_after
            m = re.search(r'"retry_after"\s*:\s*(\d+(?:\.\d+)?)', body)
            if m:
                return float(m.group(1))
        except Exception:
            pass
    if retry_after is not None:
        try:
            return float(retry_after)
        except Exception:
            pass
    return 1.0


def _exponential_backoff(attempt: int, base: float = _BACKOFF_BASE, retry_after: float = 0) -> float:
    """计算退避时间，优先使用服务端的 Retry-After"""
    if retry_after > 0:
        return retry_after
    return min(base * (2 ** attempt), 60.0)


def call_openai(payload: Dict[str, Any], timeout: Optional[int] = None, trace_id: str = "") -> urllib.response.addinfourl:
    """调用 OpenAI 上游，支持指数退避重试和熔断器"""
    if not OPENAI_BASE_URL:
        raise RuntimeError(_BASE_URL_ERROR or "OPENAI_BASE_URL invalid after sanitize")
    ok, base = sanitize_url(OPENAI_BASE_URL, kind="openai")
    if not ok:
        raise RuntimeError(base)
    url = f"{base}/chat/completions"
    to = int(timeout) if timeout is not None else UPSTREAM_TIMEOUT

    # 检查熔断器
    now = time.time()
    with _failover_lock:
        circuit_until = _failover_state.get("circuit_open_until", 0)
    if circuit_until > now:
        raise RuntimeError(f"circuit open, upstream unavailable until {datetime.fromtimestamp(circuit_until).strftime('%H:%M:%S')}")

    last_exception = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = http_json(url, payload, timeout=to, trace_id=trace_id)
            # 成功：重置故障转移状态
            _failover_state["consecutive_failures"] = 0
            return resp
        except urllib.error.HTTPError as e:
            last_exception = e
            err_body = ""
            if e.fp:
                try:
                    err_body = e.fp.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass
            err_lower = err_body.lower()

            # 400: max_tokens 兼容性重试（只重试一次）
            if e.code == 400 and "max_tokens" in payload and "max_tokens" in err_lower and attempt == 0:
                p2 = dict(payload)
                p2["max_completion_tokens"] = p2.pop("max_tokens")
                log(f"retry with max_completion_tokens (400)", "WARN", trace_id)
                try:
                    resp = http_json(url, p2, timeout=to, trace_id=trace_id)
                    return resp
                except urllib.error.HTTPError as e2:
                    if e2.code in (400, 404) and "tools" in p2 and ("tool" in err_lower or "function" in err_lower):
                        p3 = dict(p2)
                        p3.pop("tools", None)
                        p3.pop("tool_choice", None)
                        log("retry without tools (after max_completion_tokens)", "WARN", trace_id)
                        resp = http_json(url, p3, timeout=to, trace_id=trace_id)
                        return resp
                    last_exception = e2
                    continue

            # 400/404: tools 不兼容，去掉重试
            if e.code in (400, 404) and "tools" in payload and ("tool" in err_lower or "function" in err_lower):
                p2 = dict(payload)
                p2.pop("tools", None)
                p2.pop("tool_choice", None)
                log(f"retry without tools ({e.code})", "WARN", trace_id)
                resp = http_json(url, p2, timeout=to, trace_id=trace_id)
                return resp

            # 可重试的错误：429 / 5xx / 网络超时
            if e.code in (429, 500, 502, 503, 504) or is_upstream_failure(e):
                wait = _exponential_backoff(attempt, retry_after=_parse_retry_after(e))
                log(f"upstream {e.code}, retry {attempt+1}/{_MAX_RETRIES} in {wait:.1f}s", "WARN", trace_id)
                time.sleep(wait)
                _failover_state["consecutive_failures"] = _failover_state.get("consecutive_failures", 0) + 1
                continue

            # 不可重试，直接抛出
            raise urllib.error.HTTPError(e.url, e.code, err_body[:1000] or str(e.reason), e.headers, io.BytesIO(err_body.encode("utf-8")))

        except urllib.error.URLError as e:
            last_exception = e
            if is_upstream_failure(e):
                wait = _exponential_backoff(attempt)
                log(f"network error, retry {attempt+1}/{_MAX_RETRIES} in {wait:.1f}s: {e.reason}", "WARN", trace_id)
                time.sleep(wait)
                _failover_state["consecutive_failures"] = _failover_state.get("consecutive_failures", 0) + 1
                continue
            raise
        except Exception as e:
            last_exception = e
            if is_upstream_failure(e):
                wait = _exponential_backoff(attempt)
                log(f"unexpected error, retry {attempt+1}/{_MAX_RETRIES} in {wait:.1f}s: {e}", "WARN", trace_id)
                time.sleep(wait)
                _failover_state["consecutive_failures"] = _failover_state.get("consecutive_failures", 0) + 1
                continue
            raise

    # 重试耗尽，触发熔断
    _failover_state["circuit_open_until"] = time.time() + _CIRCUIT_COOLDOWN_SECONDS
    log(f"circuit OPEN for {_CIRCUIT_COOLDOWN_SECONDS}s after {_MAX_RETRIES} failures", "ERROR", trace_id)
    if last_exception:
        raise last_exception
    raise RuntimeError("upstream failed after retries")

# ── 模型工具 ──

def clean_model(model: Optional[str]) -> str:
    m = (model or DEFAULT_MODEL or "gpt-4o").strip()
    if m.endswith("[1m]"):
        m = m[:-4]
    return m



def blocks_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts_list: List[str] = []
        for b in content:
            if isinstance(b, str):
                parts_list.append(b)
            elif isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    parts_list.append(str(b.get("text") or ""))
                elif t == "input_text":
                    parts_list.append(str(b.get("text") or ""))
                elif t == "image":
                    parts_list.append("[image]")
        return "\n".join(p for p in parts_list if p)
    return str(content)

def convert_messages(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    system = body.get("system")
    if isinstance(system, str) and system.strip():
        out.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = blocks_to_text(system)
        if text.strip():
            out.append({"role": "system", "content": text})

    for msg in body.get("messages") or []:
        role = msg.get("role") or "user"
        content = msg.get("content")

        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    result = b.get("content", "")
                    if isinstance(result, list):
                        result = blocks_to_text(result)
                    elif not isinstance(result, str):
                        result = json.dumps(result, ensure_ascii=False)
                    out.append({
                        "role": "tool",
                        "tool_call_id": str(b.get("tool_use_id") or b.get("id") or ""),
                        "content": result,
                    })
                elif b.get("type") == "text" and b.get("text"):
                    out.append({"role": "user", "content": b.get("text")})
            continue

        if role == "assistant" and isinstance(content, list):
            texts: List[str] = []
            tool_calls = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(str(b.get("text") or ""))
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": str(b.get("id") or f"call_{len(tool_calls)}"),
                        "type": "function",
                        "function": {
                            "name": str(b.get("name") or "tool"),
                            "arguments": json.dumps(
                                b.get("input") if b.get("input") is not None else {},
                                ensure_ascii=False,
                            ),
                        },
                    })
            item: Dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(texts) if texts else "",
            }
            if tool_calls:
                item["tool_calls"] = tool_calls
            out.append(item)
            continue

        if isinstance(content, list):
            oai_content = []
            has_image = False
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    oai_content.append({"type": "text", "text": str(b.get("text") or "")})
                elif t == "image":
                    has_image = True
                    src = b.get("source") or {}
                    data = src.get("data") or ""
                    media = src.get("media_type") or "image/png"
                    if src.get("type") == "base64" and data:
                        oai_content.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}})
                    elif b.get("url"):
                        oai_content.append({"type": "image_url", "image_url": {"url": b["url"]}})
            if has_image or oai_content:
                out.append({"role": "assistant" if role == "assistant" else "user", "content": oai_content})
            else:
                out.append({"role": "assistant" if role == "assistant" else "user", "content": blocks_to_text(content)})
        else:
            out.append({"role": "assistant" if role == "assistant" else "user", "content": blocks_to_text(content)})
    return out

def convert_tools(body: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    tools = body.get("tools")

    # 先把 Anthropic tools 转成 OpenAI function tools
    oai: List[Dict[str, Any]] = []
    if tools:
        for t in tools:
            if not isinstance(t, dict):
                continue
            # 已是 OpenAI 形态
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn = t["function"]
                if fn.get("name"):
                    oai.append({
                        "type": "function",
                        "function": {
                            "name": fn["name"],
                            "description": fn.get("description") or "",
                            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                        },
                    })
                continue
            # Anthropic 形态: name + input_schema
            name = t.get("name")
            if not name:
                continue
            oai.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": t.get("description") or "",
                    "parameters": t.get("input_schema") or t.get("parameters") or {"type": "object", "properties": {}},
                },
            })

    # 多Agent已移除，不再注入 dispatch_task

    return oai or None

def convert_tool_choice(body: Dict[str, Any]) -> Any:
    tc = body.get("tool_choice")
    if tc is None:
        return None
    if isinstance(tc, str):
        if tc == "any":
            return "required"
        return tc
    if isinstance(tc, dict):
        t = tc.get("type")
        if t == "tool" and tc.get("name"):
            return {"type": "function", "function": {"name": tc["name"]}}
        if t == "any":
            return "required"
        if t in ("auto", "none"):
            return t
    return None

def build_payload(body: Dict[str, Any], stream: bool) -> Dict[str, Any]:
    model = clean_model(body.get("model"))
    reasoning = is_reasoning_model(model)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": convert_messages(body),
        "stream": stream,
    }
    tokens_key = "max_tokens"
    if body.get("max_tokens") is not None:
        payload[tokens_key] = body["max_tokens"]
    elif DEFAULT_MAX_TOKENS:
        # 客户端没给 → 用环境变量兜底（解决“参数没传过去”）
        try:
            payload[tokens_key] = int(DEFAULT_MAX_TOKENS)
        except Exception:
            pass
    # 大型任务：拉高推理预算 + 强化 system prompt
    if is_large_complex_task(body):
        cur = int(payload.get(tokens_key) or 0)
        if cur < HIGH_REASONING_MAX_TOKENS:
            payload[tokens_key] = HIGH_REASONING_MAX_TOKENS
        if body.get("temperature") is None and not reasoning:
            payload["temperature"] = HIGH_REASONING_TEMPERATURE
        # 强化 system
        msgs = payload.get("messages") or []
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = (str(msgs[0].get("content") or "") + "\n\n" + HIGH_REASONING_SYSTEM).strip()
        else:
            msgs = [{"role": "system", "content": HIGH_REASONING_SYSTEM}] + list(msgs)
            payload["messages"] = msgs

    # ── 采样参数：客户端优先，其次环境变量兜底 ──
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    elif DEFAULT_TEMPERATURE and "temperature" not in payload:
        try:
            payload["temperature"] = float(DEFAULT_TEMPERATURE)
        except Exception:
            pass
    if body.get("top_p") is not None:
        payload["top_p"] = body["top_p"]
    elif DEFAULT_TOP_P:
        try:
            payload["top_p"] = float(DEFAULT_TOP_P)
        except Exception:
            pass
    # 透传更多常见采样参数（之前被静默丢弃）
    for src_key, dst_key in (
        ("top_k", "top_k"),
        ("frequency_penalty", "frequency_penalty"),
        ("presence_penalty", "presence_penalty"),
        ("seed", "seed"),
        ("response_format", "response_format"),
        ("logit_bias", "logit_bias"),
    ):
        if body.get(src_key) is not None:
            payload[dst_key] = body[src_key]
    if payload.get("frequency_penalty") is None and DEFAULT_FREQUENCY_PENALTY:
        try:
            payload["frequency_penalty"] = float(DEFAULT_FREQUENCY_PENALTY)
        except Exception:
            pass
    if payload.get("presence_penalty") is None and DEFAULT_PRESENCE_PENALTY:
        try:
            payload["presence_penalty"] = float(DEFAULT_PRESENCE_PENALTY)
        except Exception:
            pass
    if payload.get("seed") is None and DEFAULT_SEED:
        try:
            payload["seed"] = int(DEFAULT_SEED)
        except Exception:
            pass
    if body.get("stop_sequences"):
        payload["stop"] = body["stop_sequences"]

    if not DROP_TOOLS:
        tools = convert_tools(body)
        if tools:
            payload["tools"] = tools
        tool_choice = convert_tool_choice(body)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    # ── 推理模型 / 普通模型 参数分流（关键）──
    _apply_model_param_policy(payload, model, reasoning)
    return payload


def _apply_model_param_policy(payload: Dict[str, Any], model: str, reasoning: Optional[bool] = None) -> None:
    """按模型类型修正参数。
    - 推理模型（OpenAI o系列/gpt-5）：只注入 reasoning_effort=high，其他参数不变
    - 普通模型 + 有 tools：开 parallel_tool_calls
    """
    if reasoning is None:
        reasoning = is_reasoning_model(model)
    if reasoning:
        # OpenAI 推理模型注入 reasoning_effort=high，其他参数保持原样
        if supports_reasoning_effort(model) and DEFAULT_REASONING_EFFORT:
            payload["reasoning_effort"] = DEFAULT_REASONING_EFFORT
    else:
        # 普通模型：Claude Code 依赖并行工具调用
        if payload.get("tools") and PARALLEL_TOOL_CALLS:
            payload.setdefault("parallel_tool_calls", True)

# ── OpenAI 响应 → Anthropic 消息 ──

def oai_msg_to_anthropic_content(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    text = message.get("content")
    if text is None:
        text = message.get("reasoning_content") or message.get("reasoning") or ""
    if isinstance(text, list):
        text = blocks_to_text(text)
    if text:
        blocks.append({"type": "text", "text": str(text)})

    for i, tc in enumerate(message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            args = {"raw": raw}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or f"toolu_{i}",
            "name": fn.get("name") or "tool",
            "input": args if isinstance(args, dict) else {"value": args},
        })
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks

def stop_reason(finish: Optional[str], has_tools: bool) -> str:
    if has_tools or finish == "tool_calls":
        return "tool_use"
    if finish == "length":
        return "max_tokens"
    return "end_turn"

def to_anthropic_message(oai: Dict[str, Any], model: str) -> Dict[str, Any]:
    choices = oai.get("choices") or []
    if not choices:
        return {
            "id": oai.get("id") or "msg_proxy",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "[proxy] upstream returned empty choices (possibly content filtered)"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    choice = choices[0]
    message = choice.get("message") or {}
    content = oai_msg_to_anthropic_content(message)
    has_tools = any(b.get("type") == "tool_use" for b in content)
    usage = oai.get("usage") or {}
    # Record token usage
    pt = usage.get("prompt_tokens") or 0
    ct = usage.get("completion_tokens") or 0
    if pt or ct:
        _record_usage(model, pt, ct)
    return {
        "id": oai.get("id") or "msg_proxy",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason(choice.get("finish_reason"), has_tools),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }

# ── SSE 工具 ──


def sse(event: str, data: Any) -> bytes:
    """编码一条 SSE 事件"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def write_stream_from_text(handler: "Handler", model: str, text: str, stop: str = "end_turn"):
    handler.wfile.write(
        sse("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_proxy", "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
    )
    handler.wfile.write(
        sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
    )
    step = 80
    for i in range(0, max(len(text), 1), step):
        piece = text[i:i + step] if text else ""
        if piece:
            handler.wfile.write(
                sse("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                })
            )
            handler.wfile.flush()
    handler.wfile.write(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
    ot = max(1, len(text.encode('utf-8')) // 3)
    handler.wfile.write(
        sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": ot},
        })
    )
    handler.wfile.write(sse("message_stop", {"type": "message_stop"}))
    handler.wfile.flush()
    _record_usage(model, 0, ot)

def _handle_non_sse_with_tools(handler: "Handler", oai: Dict[str, Any], model: str, request_body: Optional[Dict[str, Any]] = None):
    """非流式响应含 tool_use 时，拆成 SSE 事件流输出"""
    anth = to_anthropic_message(oai, model)
    has_tools = any(b.get("type") == "tool_use" for b in anth.get("content") or [])

    if not has_tools:
        text = "".join(b.get("text") or "" for b in anth.get("content") or [] if b.get("type") == "text")
        write_stream_from_text(handler, model, text or "")
        return

    handler.wfile.write(
        sse("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_proxy", "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
    )
    handler.wfile.flush()

    next_index = 0
    opened = set()
    text_index = None

    for i, b in enumerate(anth.get("content") or []):
        if b.get("type") == "text":
            if text_index is None:
                text_index = next_index
                next_index += 1
                opened.add(text_index)
                handler.wfile.write(
                    sse("content_block_start", {
                        "type": "content_block_start", "index": text_index,
                        "content_block": {"type": "text", "text": ""},
                    })
                )
                handler.wfile.flush()
            handler.wfile.write(
                sse("content_block_delta", {
                    "type": "content_block_delta", "index": text_index,
                    "delta": {"type": "text_delta", "text": b.get("text") or ""},
                })
            )
            handler.wfile.flush()
        elif b.get("type") == "tool_use":
            bi = next_index
            next_index += 1
            opened.add(bi)
            handler.wfile.write(
                sse("content_block_start", {
                    "type": "content_block_start", "index": bi,
                    "content_block": {
                        "type": "tool_use",
                        "id": b.get("id") or f"toolu_{i}",
                        "name": b.get("name") or "tool", "input": {},
                    },
                })
            )
            handler.wfile.flush()
            handler.wfile.write(
                sse("content_block_delta", {
                    "type": "content_block_delta", "index": bi,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(b.get("input") or {}, ensure_ascii=False),
                    },
                })
            )
            handler.wfile.flush()

    for idx in sorted(opened):
        handler.wfile.write(sse("content_block_stop", {"type": "content_block_stop", "index": idx}))
    handler.wfile.write(
        sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": anth.get("stop_reason") or "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        })
    )
    handler.wfile.write(sse("message_stop", {"type": "message_stop"}))
    handler.wfile.flush()

def convert_openai_sse(handler: "Handler", resp, model: str, request_body: Optional[Dict[str, Any]] = None) -> None:
    """把 OpenAI SSE 转成 Anthropic SSE。支持流式和非流式两种输入。"""
    text_parts: List[str] = []
    tool_states: Dict[int, Dict[str, Any]] = {}
    text_index: Optional[int] = None
    next_index = 0
    finish: Optional[str] = None
    opened = set()
    started = False

    def ensure_message_start():
        nonlocal started
        if started:
            return
        started = True
        handler.wfile.write(
            sse("message_start", {
                "type": "message_start",
                "message": {
                    "id": "msg_proxy_stream", "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            })
        )
        handler.wfile.flush()

    def ensure_text():
        nonlocal text_index, next_index
        ensure_message_start()
        if text_index is not None:
            return
        text_index = next_index
        next_index += 1
        opened.add(text_index)
        handler.wfile.write(
            sse("content_block_start", {
                "type": "content_block_start", "index": text_index,
                "content_block": {"type": "text", "text": ""},
            })
        )
        handler.wfile.flush()

    def ensure_tool(idx: int, tc_id: str, name: str):
        nonlocal next_index
        ensure_message_start()
        if idx in tool_states:
            return tool_states[idx]
        bi = next_index
        next_index += 1
        st = {"block_index": bi, "id": tc_id, "name": name}
        tool_states[idx] = st
        opened.add(bi)
        handler.wfile.write(
            sse("content_block_start", {
                "type": "content_block_start", "index": bi,
                "content_block": {
                    "type": "tool_use", "id": tc_id, "name": name, "input": {},
                },
            })
        )
        handler.wfile.flush()
        return st

    # ── 非流式响应：完整 JSON ──
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in content_type:
        raw = resp.read().decode("utf-8", errors="ignore")
        resp.close()
        if DEBUG:
            log(f"non-sse response ct={content_type} head={raw[:200]!r}")
        try:
            oai = json.loads(raw)
            if oai.get("error"):
                err = oai["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                log(f"upstream error json: {msg}", "ERROR")
                ensure_message_start()
                write_stream_from_text(handler, model, f"[upstream error] {msg}")
                return
            _handle_non_sse_with_tools(handler, oai, model, request_body=request_body)
        except json.JSONDecodeError as e:
            log(f"non-sse json parse fail: {e}", "ERROR")
            ensure_message_start()
            write_stream_from_text(handler, model, f"[proxy] upstream returned non-json: {raw[:300]}")
        except Exception as e:
            log(f"non-sse convert fail: {e}\n{traceback.format_exc()}", "ERROR")
            ensure_message_start()
            write_stream_from_text(handler, model, f"[proxy error] {e}")
        return

    # ── 流式响应 ──
    while True:
        line = resp.readline()
        if not line:
            break
        try:
            s = line.decode("utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not s or s.startswith(":"):
            continue
        if not s.startswith("data:"):
            continue
        data = s[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except Exception:
            continue

        if chunk.get("error"):
            msg = chunk["error"]
            if isinstance(msg, dict):
                msg = msg.get("message") or json.dumps(msg, ensure_ascii=False)
            ensure_message_start()
            write_stream_from_text(handler, model, f"[upstream error] {msg}")
            return

        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if choice.get("finish_reason"):
            finish = choice.get("finish_reason")

        piece = delta.get("content")
        if piece:
            text_parts.append(piece)
            ensure_text()
            handler.wfile.write(
                sse("content_block_delta", {
                    "type": "content_block_delta", "index": text_index,
                    "delta": {"type": "text_delta", "text": piece},
                })
            )
            handler.wfile.flush()

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            st = ensure_tool(
                idx,
                tc.get("id") or f"toolu_{idx}",
                fn.get("name") or (tool_states.get(idx) or {}).get("name") or "tool",
            )
            if tc.get("id"):
                st["id"] = tc["id"]
            if fn.get("name"):
                st["name"] = fn["name"]
            args_piece = fn.get("arguments") or ""
            if args_piece:
                handler.wfile.write(
                    sse("content_block_delta", {
                        "type": "content_block_delta", "index": st["block_index"],
                        "delta": {"type": "input_json_delta", "partial_json": args_piece},
                    })
                )
                handler.wfile.flush()

    ensure_message_start()
    if not opened:
        write_stream_from_text(
            handler, model,
            "[proxy] upstream returned empty content. check model name / API key / base url.",
        )
        return

    for idx in sorted(opened):
        handler.wfile.write(sse("content_block_stop", {"type": "content_block_stop", "index": idx}))
    ot = max(0, len("".join(text_parts).encode('utf-8')) // 3)
    if ot == 0 and tool_states:
        ot = 1
    handler.wfile.write(
        sse("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason(finish, bool(tool_states)),
                "stop_sequence": None,
            },
            "usage": {"output_tokens": ot},
        })
    )
    handler.wfile.write(sse("message_stop", {"type": "message_stop"}))
    handler.wfile.flush()
    _record_usage(model, 0, ot)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        log(f"{self.command} {self.path} - {fmt % args}")

    def _read_json(self) -> Dict[str, Any]:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            raw = b""
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                size = int(line.strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                raw += self.rfile.read(size)
                self.rfile.readline()
        else:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        conn = (self.headers.get("Connection") or "").lower()
        keep = "close" not in conn and self.protocol_version == "HTTP/1.1"
        self.send_header("Connection", "keep-alive" if keep else "close")
        self.end_headers()
        self.wfile.write(body)

    def _check_upstream(self) -> Optional[Dict[str, Any]]:
        if not OPENAI_BASE_URL:
            return {"reachable": False, "error": "OPENAI_BASE_URL not configured"}
        try:
            test_url = f"{OPENAI_BASE_URL}/models"
            req = urllib.request.Request(
                test_url,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "User-Agent": "claude-code-proxy/2.0"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return {"reachable": True, "status": r.status}
        except urllib.error.HTTPError as e:
            return {"reachable": False, "status": e.code, "error": str(e.reason)[:200]}
        except Exception as e:
            return {"reachable": False, "error": str(e)[:200]}

    def do_HEAD(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/health", "/v1/models"):
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            conn = (self.headers.get("Connection") or "").lower()
            keep = "close" not in conn and self.protocol_version == "HTTP/1.1"
            self.send_header("Connection", "keep-alive" if keep else "close")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/health"):
            upstream = self._check_upstream()
            # 添加故障转移状态
            with _failover_lock:
                circuit_until = _failover_state.get("circuit_open_until", 0)
                consecutive = _failover_state.get("consecutive_failures", 0)
            circuit_open = circuit_until > time.time()
            self._send_json(200, {
                "ok": True,
                "service": "anthropic-to-openai-proxy",
                "openai_base_url": OPENAI_BASE_URL,
                "default_model": DEFAULT_MODEL,
                "has_key": bool(OPENAI_API_KEY),
                "upstream": upstream,
                "version": "2.1",
                "failover": {
                    "circuit_open": circuit_open,
                    "consecutive_failures": consecutive,
                    "retry_in": max(0, int(circuit_until - time.time())) if circuit_open else 0,
                },
            })
            return
        if path == "/v1/models":
            # 代理真实上游模型列表；失败则退回默认模型
            try:
                ok, base = sanitize_url(OPENAI_BASE_URL, kind="openai")
                if ok and OPENAI_API_KEY:
                    req = urllib.request.Request(
                        f"{base}/models",
                        method="GET",
                        headers={
                            "Authorization": f"Bearer {OPENAI_API_KEY}",
                            "User-Agent": "claude-code-openai-proxy/2.1",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=15) as r:
                        upstream_models = json.loads(r.read().decode("utf-8", errors="ignore"))
                    if isinstance(upstream_models, dict) and upstream_models.get("data"):
                        self._send_json(200, upstream_models)
                        return
            except Exception as e:
                log(f"/v1/models upstream fetch fail: {e}", "WARN")
            self._send_json(200, {
                "object": "list",
                "data": [{"id": DEFAULT_MODEL, "object": "model"}],
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]

        if "count_tokens" in path:
            try:
                body = self._read_json()
            except Exception:
                body = {}
            self._send_json(200, {"input_tokens": max(1, len(json.dumps(body).encode('utf-8')) // 3)})
            return

        if not (path.endswith("/v1/messages") or path.endswith("/messages")):
            self._send_json(404, {"type": "error", "error": {"type": "not_found", "message": path}})
            return

        if not OPENAI_API_KEY:
            self._send_json(500, {
                "type": "error",
                "error": {"type": "api_error", "message": "OPENAI_API_KEY not set in proxy process"},
            })
            return

        trace_id = _make_trace_id()

        try:
            body = self._read_json()
        except Exception as e:
            self._send_json(400, {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": str(e)},
            })
            return

        want_stream = bool(body.get("stream"))
        use_stream_upstream = want_stream and not FORCE_NON_STREAM
        model = clean_model(body.get("model"))
        if DROP_TOOLS:
            body = dict(body)
            body.pop("tools", None)
            body.pop("tool_choice", None)
        payload = build_payload(body, stream=use_stream_upstream)
        if DEBUG:
            log(
                f"client_stream={want_stream} upstream_stream={use_stream_upstream} "
                f"drop_tools={DROP_TOOLS} model={model} msgs={len(payload.get('messages') or [])}",
                trace_id=trace_id,
            )

        # 主模型调用
        resp = None
        try:
            resp = call_openai(payload, trace_id=trace_id)
        except urllib.error.HTTPError as e:
            msg = (e.fp.read().decode("utf-8", errors="ignore") if e.fp else "") or str(e.reason)
            log(f"upstream HTTPError: {e.code} {msg[:500]}", "ERROR", trace_id)
            self._send_json(e.code if 400 <= e.code < 600 else 500, {
                "type": "error",
                "error": {"type": "api_error", "message": f"OpenAI upstream {e.code}: {msg[:1000]}"},
            })
            return
        except Exception as e:
            log(f"request failed: {e}\n{traceback.format_exc()}", "ERROR", trace_id)
            self._send_json(500, {
                "type": "error",
                "error": {"type": "api_error", "message": f"proxy request failed: {e}"},
            })
            return

        # ── 客户端要流式 ──
        if want_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                if use_stream_upstream:
                    convert_openai_sse(self, resp, model, request_body=body)
                else:
                    raw = resp.read().decode("utf-8", errors="ignore")
                    if DEBUG:
                        log(f"upstream non-stream raw head={raw[:400]!r}")
                    try:
                        oai = json.loads(raw)
                    except json.JSONDecodeError as e:
                        log(f"upstream json parse fail: {e}", "ERROR")
                        write_stream_from_text(self, model, f"[proxy] upstream returned non-json: {raw[:300]}")
                        return
                    if oai.get("error"):
                        err = oai["error"]
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        log(f"upstream error: {msg}", "ERROR")
                        write_stream_from_text(self, model, f"[upstream error] {msg}")
                        return
                    _handle_non_sse_with_tools(self, oai, model, request_body=body)
            except (BrokenPipeError, ConnectionResetError):
                log("client disconnected during stream", "WARN")
            except Exception as e:
                log(f"stream convert error: {e}\n{traceback.format_exc()}", "ERROR")
                try:
                    write_stream_from_text(self, model, f"[proxy stream error] {e}")
                except Exception:
                    pass
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            return

        # ── 客户端非流式 ──
        try:
            raw = resp.read().decode("utf-8", errors="ignore")
            oai = json.loads(raw)
            anth = to_anthropic_message(oai, model)
            
            if DEBUG:
                log(f"non-stream ok stop={anth.get('stop_reason')} blocks={len(anth.get('content') or [])}")
            self._send_json(200, anth)
        except Exception as e:
            log(f"convert failed: {e}\n{traceback.format_exc()}", "ERROR")
            self._send_json(500, {"type": "error", "error": {"type": "api_error", "message": f"convert failed: {e}"}})
        finally:
            try:
                resp.close()
            except Exception:
                pass


def main() -> None:
    if not OPENAI_API_KEY:
        log("WARNING: OPENAI_API_KEY empty", "WARN")
    if not OPENAI_BASE_URL:
        log(f"ERROR: bad OPENAI_BASE_URL: {_BASE_URL_ERROR}", "ERROR")
    else:
        log(f"openai_base={OPENAI_BASE_URL}")
    log(f"listen http://{PROXY_HOST}:{PROXY_PORT}")
    log(f"default_model={DEFAULT_MODEL}")
    log(f"force_non_stream={FORCE_NON_STREAM} drop_tools={DROP_TOOLS}")
    log(f"retry={_MAX_RETRIES} backoff={_BACKOFF_BASE}s circuit={_CIRCUIT_FAILURE_THRESHOLD}/{_CIRCUIT_COOLDOWN_SECONDS}s")
    server = ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), Handler)
    server.timeout = 30
    server.socket.settimeout(30)

    # 优雅关闭
    def signal_handler(sig, frame):
        log("shutting down gracefully...")
        server.shutdown()
        server.server_close()
        _conn_pool.invalidate("__shutdown__")
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("bye")
        server.server_close()

if __name__ == "__main__":
    main()
