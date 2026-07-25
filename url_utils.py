#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==========================================================
# @xiaozhe - Termux Claude Code OpenAI Proxy
# 版权所有 © 2026 小哲
# ==========================================================
"""
URL 清洗公共模块 — 被 claude.py 和 openai_proxy.py 共用。
修正用户常见输入错误：
  "https://token sensenova.cn/v1" → "https://token.sensenova.cn/v1"
"""

import re
import urllib.parse
from typing import Tuple


def sanitize_url(raw: str, kind: str = "openai") -> Tuple[bool, str]:
    """
    清洗 URL。
    自动把域名中的空格变成点；保留 /v1；不判非法、不改成 api.xxx.cn。
    返回 (ok, cleaned_or_error_message)
    """
    if raw is None:
        return False, "URL 为空"
    s = str(raw)
    s = s.replace("\ufeff", "").replace("\u200b", "").replace("\u00a0", " ")
    s = s.strip()
    s = re.sub(r"\s+", " ", s)

    # 抽出 http(s):// 开头部分
    m2 = re.search(r"(https?://.+)", s, flags=re.I)
    if m2:
        s = m2.group(1).strip()
    s = s.rstrip("/")

    # 把 host 段里的空格变成点
    m3 = re.match(r"^(https?://)([^/?#]+)(.*)$", s, flags=re.I)
    if m3:
        scheme, hostport, rest = m3.group(1), m3.group(2), m3.group(3)
        userinfo = ""
        if "@" in hostport:
            userinfo, hostport = hostport.rsplit("@", 1)
            userinfo = userinfo + "@"
        port = ""
        if ":" in hostport and hostport.count(":") == 1:
            host_only, port_maybe = hostport.split(":", 1)
            if port_maybe.isdigit():
                hostport, port = host_only, ":" + port_maybe
        if re.search(r"\s", hostport):
            fixed_host = re.sub(r"\s+", ".", hostport.strip())
            fixed_host = re.sub(r"\.+", ".", fixed_host).strip(".")
            hostport = fixed_host
        s = f"{scheme}{userinfo}{hostport}{port}{rest}"

    if not (s.startswith("http://") or s.startswith("https://")):
        if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/.*)?$", s):
            s = "https://" + s
        else:
            return False, "URL 必须以 http:// 或 https:// 开头"

    try:
        u = urllib.parse.urlsplit(s)
    except Exception as e:
        return False, f"URL 解析失败: {e}"

    host = u.hostname or ""
    if not host:
        return False, "URL 缺少域名"
    if re.search(r"\s", host):
        host = re.sub(r"\s+", ".", host.strip())
        host = re.sub(r"\.+", ".", host).strip(".")
        netloc = host
        if u.port:
            netloc = f"{host}:{u.port}"
        u = urllib.parse.SplitResult(u.scheme, netloc, u.path, u.query, u.fragment)

    path = u.path or ""
    if kind == "openai":
        if not path or path == "/":
            path = "/v1"
    cleaned = urllib.parse.urlunsplit(
        (u.scheme, u.netloc, path.rstrip("/"), "", "")
    )

    if re.search(r"\s", cleaned):
        cleaned = re.sub(r"\s+", "", cleaned)

    host2 = urllib.parse.urlsplit(cleaned).hostname or ""
    if not host2:
        return False, f"域名非法: {cleaned!r}"

    return True, cleaned


def sanitize_key(raw: str) -> Tuple[bool, str]:
    """清洗 API Key：去空格、去零宽字符、去误粘的 Bearer 前缀"""
    if raw is None:
        return False, "Key 为空"
    k = str(raw).replace("\ufeff", "").replace("\u200b", "").strip()
    k = re.sub(r"\s+", "", k)
    if k.lower().startswith("bearer"):
        k = k[6:].lstrip(":").strip()
    if not k:
        return False, "Key 为空"
    if re.search(r"\s", k):
        return False, "Key 不能含空格"
    return True, k


def sanitize_model(raw: str, default: str = "gpt-4o") -> str:
    """清洗模型名，支持别名"""
    m = (raw or default).replace("\ufeff", "").strip()
    m = re.sub(r"\s+", "", m)
    aliases = {
        "grok": "grok-4.5",
        "grok4.5": "grok-4.5",
        "grok-4": "grok-4.5",
        "deepseek-v4": "deepseek-v4-pro",
    }
    ml = m.lower()
    if ml in aliases:
        m = aliases[ml]
    return m or default