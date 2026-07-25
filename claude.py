#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==========================================================
# @xiaozhe - Termux Claude Code OpenAI Proxy
# 版权所有 © 2026 小哲
# ==========================================================
"""
Claude Code Termux 启动器 + OpenAI 本地代理管理
- 严格清洗/校验 Base URL，禁止空格等非法字符
- 只写 ANTHROPIC_AUTH_TOKEN，避免 Auth conflict
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import hashlib
import shutil

# 代理操作互斥锁（守护线程与菜单操作竞争防护）
_proxy_op_lock = threading.Lock()

# 从公共模块导入 URL/Key 清洗函数
from url_utils import sanitize_url, sanitize_key, sanitize_model

HOME = os.environ.get("HOME", "/data/data/com.termux/files/home")
PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
DIR = os.path.join(HOME, "Claudecode")
CC = os.path.join(DIR, "cc.py")
PROXY = os.path.join(DIR, "openai_proxy.py")
PZWJ = os.path.join(HOME, ".claude", "settings.json")
PROXY_META = os.path.join(HOME, ".claude", "openai_proxy.json")
PROXY_LOG = os.path.join(HOME, ".claude", "openai_proxy.log")
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8765
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"
PROXY_PORT_BASE = 18765  # 多代理起始端口
# 预设总数上限：存储/切换不设小限制（可用环境变量 CC_MAX_PRESETS 调大）。
# openai 预设端口 = BASE + index。存几百个、逐个切换都没问题。
MAX_PRESETS = int(os.environ.get("CC_MAX_PRESETS") or "200")
PROXY_PORT_MAX = PROXY_PORT_BASE + MAX_PRESETS - 1

# 故障转移预设文件（与 openai_proxy.py 共享）
_FAILOVER_PRESETS_FILE = os.path.join(HOME, ".claude", "failover_presets.json")
# 配置备份目录
_BACKUP_DIR = os.path.join(HOME, ".claude", "backups")
# 模型预设文件最后修改时间缓存（热重载）
_MODEL_PRESETS_MTIME = 0.0


def allowed_proxy_ports(presets=None):
    """当前合法本地代理端口集合：8765 + 预设 openai 端口 + settings 当前端口"""
    ports = {int(PROXY_PORT)}
    if presets is None:
        try:
            presets = load_model_presets()
        except Exception:
            presets = []
    for i, p in enumerate(presets or []):
        if p.get("mode") == "openai" and 0 <= i < MAX_PRESETS:
            ports.add(PROXY_PORT_BASE + i)
    try:
        cur = parse_local_proxy_port()
        if cur is not None:
            ports.add(int(cur))
    except Exception:
        pass
    return ports


def can_add_preset(presets=None, replacing=False):
    """是否还能新增预设（覆盖重名不算新增）"""
    if replacing:
        return True
    if presets is None:
        presets = load_model_presets()
    return len(presets) < MAX_PRESETS



def proxy_meta_path(port=None):
    """meta 文件路径：默认端口用 openai_proxy.json，其它端口用 openai_proxy_<port>.json"""
    actual = int(port) if port is not None else PROXY_PORT
    if actual == PROXY_PORT:
        return PROXY_META
    return PROXY_META.replace(".json", f"_{actual}.json")


def proxy_log_path(port=None):
    """日志路径：默认端口用 openai_proxy.log，其它端口用 openai_proxy_<port>.log"""
    actual = int(port) if port is not None else PROXY_PORT
    if actual == PROXY_PORT:
        return PROXY_LOG
    return PROXY_LOG.replace(".log", f"_{actual}.log")


def parse_local_proxy_port(url=None):
    """从 ANTHROPIC_BASE_URL 解析本地代理端口；非本地则返回 None"""
    if url is None:
        url = (load_settings().get("env", {}) or {}).get("ANTHROPIC_BASE_URL") or ""
    base = (url or "").strip().rstrip("/")
    low = base.lower()
    if "127.0.0.1" not in low and "localhost" not in low:
        return None
    # http://127.0.0.1:18771 或 http://localhost:18771/v1
    try:
        # 去掉 scheme
        rest = base.split("://", 1)[-1]
        hostport = rest.split("/", 1)[0]
        if ":" in hostport:
            port_str = hostport.rsplit(":", 1)[-1]
            if port_str.isdigit():
                return int(port_str)
    except Exception:
        pass
    return PROXY_PORT


def current_proxy_port():
    """当前 settings 里本地代理端口；非代理模式返回默认 PROXY_PORT"""
    p = parse_local_proxy_port()
    return p if p is not None else PROXY_PORT


def is_local_proxy_url(url: str) -> bool:
    low = (url or "").strip().rstrip("/").lower()
    return ("127.0.0.1" in low) or ("localhost" in low)


def _listening_pids_on_port(port: int):
    """反查监听/绑定该端口的 openai_proxy PID。

    本机 Termux 上 /proc/net/tcp 常 Permission denied，ss/lsof 也没有。
    可靠做法：扫 /proc/*/cmdline 找 openai_proxy.py，再读 environ 的 PROXY_PORT=。
    """
    port = int(port)
    port_s = str(port)
    pids = []
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            # 1) cmdline 必须是 openai_proxy
            try:
                cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\x00", b" ").decode("utf-8", "ignore")
            except Exception:
                continue
            if "openai_proxy.py" not in cmd:
                continue
            # 2) environ 里 PROXY_PORT 匹配
            matched = False
            try:
                env_raw = open(f"/proc/{pid}/environ", "rb").read().split(b"\x00")
                for item in env_raw:
                    if item.startswith(b"PROXY_PORT="):
                        val = item.split(b"=", 1)[1].decode("utf-8", "ignore").strip()
                        if val == port_s:
                            matched = True
                        break
            except Exception:
                # environ 不可读时：若只有一个代理且健康检查对上，仍不可盲目杀；跳过
                matched = False
            if matched:
                pids.append(pid)
    except Exception:
        pass
    return pids


def _all_openai_proxy_pids():
    """所有 openai_proxy.py 进程 PID（用于菜单 5 全停）"""
    pids = []
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\x00", b" ").decode("utf-8", "ignore")
            except Exception:
                continue
            if "openai_proxy.py" in cmd:
                pids.append(pid)
    except Exception:
        pass
    return pids


def _kill_pids(pids, wait_s=2.0):
    """SIGTERM → 等待 → SIGKILL；返回是否至少杀过一个"""
    killed_any = False
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed_any = True
        except (ProcessLookupError, PermissionError, OSError):
            continue
    deadline = time.time() + wait_s
    alive = set(int(p) for p in pids)
    while alive and time.time() < deadline:
        time.sleep(0.1)
        gone = []
        for pid in list(alive):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone.append(pid)
            except (PermissionError, OSError):
                pass
        for pid in gone:
            alive.discard(pid)
    for pid in list(alive):
        try:
            os.kill(pid, signal.SIGKILL)
            killed_any = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return killed_any



DEFAULT_LIMITS = {
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
}



def ensure_dirs():
    os.makedirs(os.path.dirname(PZWJ), exist_ok=True)
    os.makedirs(DIR, exist_ok=True)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _models_endpoint(url: str) -> str:
    """智能拼接 /v1/models 端点，避免 /v1/v1/models 双重路径。
    - 已含 /v1/models → 原样返回
    - 已含 /v1 → 追加 /models
    - 其他 → 追加 /v1/models
    """
    u = (url or "").rstrip("/")
    if u.endswith("/v1/models"):
        return u
    if u.endswith("/v1"):
        return u + "/models"
    return u + "/v1/models"


def save_json(path, data):
    """保存 JSON，文件权限设为仅自己可读（保护 API Key）。原子写：写临时文件后 rename。"""
    ensure_dirs()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(tmp_path, 0o600)
    except Exception:
        pass
    os.replace(tmp_path, path)


def load_settings():
    cfg = load_json(PZWJ, None)
    if not isinstance(cfg, dict):
        cfg = {}
    if not isinstance(cfg.get("env"), dict):
        cfg["env"] = {}
    return cfg


def apply_default_limits(force=False):
    cfg = load_settings()
    env = cfg["env"]
    changed = []
    for k, v in DEFAULT_LIMITS.items():
        if force or not env.get(k):
            if env.get(k) != str(v):
                env[k] = str(v)
                changed.append(f"{k}={v}")
    if "ANTHROPIC_API_KEY" in env:
        env.pop("ANTHROPIC_API_KEY", None)
        changed.append("removed ANTHROPIC_API_KEY")
    ob = env.get("OPENAI_BASE_URL")
    if ob:
        ok, cleaned = sanitize_url(ob, kind="openai")
        if ok and cleaned != ob:
            env["OPENAI_BASE_URL"] = cleaned
            changed.append(f"fixed OPENAI_BASE_URL -> {cleaned}")
        elif not ok:
            print(f"\033[31m警告: 已保存的 OPENAI_BASE_URL 非法: {ob!r}\033[0m")
            print(f"\033[31m  {cleaned}\033[0m")
    if changed:
        save_json(PZWJ, cfg)
    return changed


# ── 模型预设管理 ──
MODEL_PRESETS_FILE = os.path.join(os.path.dirname(PZWJ), "model_presets.json")


def load_model_presets():
    """读取模型预设列表"""
    data = load_json(MODEL_PRESETS_FILE, [])
    return data if isinstance(data, list) else []


def save_model_presets(presets):
    """保存模型预设列表（硬上限 MAX_PRESETS）"""
    if not isinstance(presets, list):
        presets = []
    if len(presets) > MAX_PRESETS:
        print(f"\033[31m预设超过上限 {MAX_PRESETS}，已截断保留前 {MAX_PRESETS} 个\033[0m")
        presets = presets[:MAX_PRESETS]
    save_json(MODEL_PRESETS_FILE, presets)
    try:
        os.chmod(MODEL_PRESETS_FILE, 0o600)
    except Exception:
        pass
    # 多Agent已废弃，不再写 agent_presets.json


def add_model_preset_interactive():
    """交互式添加模型预设。
    - OpenAI 代理模式：只需地址 + API Key，自动拉取 /v1/models 并批量建预设（名字自动取，不设上限）。
    - Anthropic 直连模式：保留原流程（名字 + URL + Key + 模型名）。
    """
    print("\n\033[1;36m--- 添加模型预设 ---\033[0m")
    print("\n模式选择：")
    print("1. Anthropic 直连（DeepSeek/MiMo/智谱/豆包等）")
    print("2. OpenAI 代理模式（只需地址+Key，自动拉取模型列表）")
    mode = input("请选择（1/2）：").strip()

    if mode == "2":
        # ── OpenAI 模式：自动拉模型，批量建预设 ──
        u = input("OpenAI 兼容 Base URL（如 https://api.openai.com/v1）：").strip()
        ok, url = sanitize_url(u, kind="openai")
        if not ok:
            print(f"\033[31mURL 错误: {url}\033[0m")
            return
        key = input("API Key：").strip()
        ok, key = sanitize_key(key)
        if not ok:
            print(f"\033[31m{key}\033[0m")
            return

        print("\033[33m⏳ 正在拉取模型列表...\033[0m")
        models = fetch_openai_models(url, key)
        if not models:
            print("\n\033[31m未能获取到模型列表。请确认地址/Key 正确且支持 GET /v1/models。\033[0m")
            fb = input("是否手动输入一个模型名继续？(y/n, 默认 n)：").strip().lower()
            if fb != "y":
                return
            mid = input("模型名（如 gpt-4o）：").strip()
            if not mid:
                return
            models = [{"id": mid, "owned_by": ""}]

        presets = load_model_presets()
        existing_names = {p.get("name") for p in presets if p.get("name")}
        existing_ids = {p.get("model") for p in presets if p.get("model")}
        added = 0
        skipped = 0
        for m in models:
            mid = m["id"]
            if mid in existing_ids:
                skipped += 1
                continue
            name = mid
            if name in existing_names:
                suffix = 1
                while f"{name}_{suffix}" in existing_names:
                    suffix += 1
                name = f"{name}_{suffix}"
            presets.append({
                "name": name,
                "mode": "openai",
                "openai_base_url": url,
                "api_key": key,
                "model": mid,
            })
            existing_names.add(name)
            existing_ids.add(mid)
            added += 1

        if added == 0:
            print(f"\n\033[33m没有新增模型（拉到 {len(models)} 个，全部已存在）\033[0m")
            return
        save_model_presets(presets)
        print(f"\n\033[32m✅ 成功导入 {added} 个模型预设！\033[0m")
        if skipped:
            print(f"\033[33m（跳过 {skipped} 个已存在的模型）\033[0m")
        print(f"   当前共 {len(presets)} 个预设")
        if input("\n\033[33m立即切换到第一个新模型？(y/n, 默认 n)：\033[0m").strip().lower() == "y":
            apply_model_preset(len(presets) - added)
        return

    # ── Anthropic 直连模式：保留原流程 ──
    name = input("预设名称（如 豆包写代码）：").strip()
    if not name:
        print("\033[31m名称不能为空\033[0m")
        return
    u = input("Anthropic 兼容 URL：").strip()
    ok, url = sanitize_url(u, kind="anthropic")
    if not ok:
        print(f"\033[31mURL 错误: {url}\033[0m")
        return
    key = input("API Key：").strip()
    ok, key = sanitize_key(key)
    if not ok:
        print(f"\033[31m{key}\033[0m")
        return
    model = sanitize_model(input("模型名：").strip() or "gpt-4o")
    preset = {
        "name": name,
        "mode": "direct",
        "anthropic_base_url": url,
        "api_key": key,
        "model": model,
    }

    presets = load_model_presets()
    replacing = False
    for p in presets:
        if p.get("name") == name:
            if input(f"\033[33m预设「{name}」已存在，覆盖？(y/n)：\033[0m").strip().lower() != "y":
                return
            presets.remove(p)
            replacing = True
            break
    if not can_add_preset(presets, replacing=replacing):
        print(f"\033[31m预设已达上限 {MAX_PRESETS} 个，请先删除再用\033[0m")
        return

    presets.append(preset)
    save_model_presets(presets)
    print(f"\033[32m预设「{name}」已保存！({len(presets)}/{MAX_PRESETS})\033[0m")


def delete_model_preset(index):
    """删除指定索引的预设；若是 openai 会先停该端口，并重排后续端口前清理残留"""
    presets = load_model_presets()
    if not (0 <= index < len(presets)):
        return False
    name = presets[index].get("name", "未知")
    was_openai = presets[index].get("mode") == "openai"
    old_ports = []
    for i, p in enumerate(presets):
        if p.get("mode") == "openai" and 0 <= i < MAX_PRESETS:
            old_ports.append(PROXY_PORT_BASE + i)
    # 先停被删项端口
    if was_openai and 0 <= index < MAX_PRESETS:
        stop_proxy(port=PROXY_PORT_BASE + index, quiet=True)
    presets.pop(index)
    save_model_presets(presets)
    # 端口按 index 重排：停掉旧 openai 端口集合中不再合法的
    new_ports = allowed_proxy_ports(presets)
    for port in old_ports:
        if port not in new_ports and proxy_running(port):
            stop_proxy(port=port, quiet=True)
    print(f"\033[33m已删除预设「{name}」（{len(presets)}/{MAX_PRESETS}）\033[0m")
    return True


def get_preset_port(index):
    """获取预设的代理端口（代理模式专用）。index 必须在 [0, MAX_PRESETS)。非法返回 None。"""
    try:
        idx = int(index)
    except Exception:
        return None
    if not (0 <= idx < MAX_PRESETS):
        return None
    return PROXY_PORT_BASE + idx


def fetch_openai_models(base_url: str, api_key: str) -> list:
    """调上游 /v1/models 获取可用模型列表。
    返回 [{'id':..., 'name':...}, ...]；失败返回 [] 并在 stderr 打印原因。
    """
    import urllib.request, urllib.error
    ok, clean = sanitize_url(base_url, kind="openai")
    if not ok:
        print(f"\033[31mURL 非法: {clean}\033[0m", file=sys.stderr)
        return []
    api_url = f"{clean}/models"
    req = urllib.request.Request(
        api_url,
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "claude-code-launcher/2.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as e:
        body = e.fp.read().decode("utf-8", errors="ignore")[:300] if e.fp else ""
        print(f"\033[31m上游 /v1/models 返回 {e.code}: {body}\033[0m", file=sys.stderr)
        return []
    except Exception as e:
        print(f"\033[31m拉取模型列表失败: {e}\033[0m", file=sys.stderr)
        return []

    raw_list = data.get("data") if isinstance(data, dict) else data if isinstance(data, list) else []
    if not raw_list:
        print("\033[33m上游返回的模型列表为空\033[0m", file=sys.stderr)
        return []

    models = []
    seen_ids = set()
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or ""
        if not mid or mid in seen_ids:
            continue
        owned = item.get("owned_by") or ""
        # 过滤掉非 LLM 模型（embedding/whisper/image/text-embedding）
        skip_patterns = (
            "embedding", "whisper", "tts", "davinci", "ada", "babbage", "curie",
            "moderation", "image", "dall-e", "transcribe", "vision",
        )
        if any(p in mid.lower() for p in skip_patterns):
            continue
        models.append({"id": mid, "owned_by": owned})
        seen_ids.add(mid)
    return models


def import_openai_models_interactive():
    """交互式：输入地址+API Key → 自动拉模型列表 → 批量创建预设（不设上限约束）。"""
    print("\n" + "=" * 60)
    print("\033[1;36m批量导入 OpenAI 兼容模型\033[0m")
    print("输入地址和 API Key 后，自动拉取 /v1/models 列表，")
    print("拉到几个就自动建几个预设（可跨预设切换器逐个切换使用）。")
    print("=" * 60 + "\n")
    u = input("OpenAI 兼容 Base URL（如 https://api.openai.com/v1）：").strip()
    key = input("API Key：").strip()
    if not u or not key:
        print("\033[31m地址和 Key 不能为空\033[0m")
        return
    ok_url, clean_url = sanitize_url(u, kind="openai")
    if not ok_url:
        print(f"\033[31mURL 错误: {clean_url}\033[0m")
        return
    ok_key, clean_key = sanitize_key(key)
    if not ok_key:
        print(f"\033[31m{clean_key}\033[0m")
        return

    print("\033[33m⏳ 正在拉取模型列表...\033[0m")
    models = fetch_openai_models(clean_url, clean_key)
    if not models:
        print("\n\033[31m未能获取到可用模型。请确认：\033[0m")
        print("  1) 地址和 API Key 正确")
        print("  2) 该端点支持 GET /v1/models")
        print("  3) 网络可到达")
        # 回退：让用户手动输入一个模型名
        fallback = input("\n是否手动输入一个模型名继续？(y/n, 默认 n)：").strip().lower()
        if fallback == "y":
            mid = input("模型名（如 gpt-4o）：").strip()
            if not mid:
                return
            models = [{"id": mid, "owned_by": ""}]
        else:
            return

    # 读取现预设，准备追加
    presets = load_model_presets()
    existing_names = {p.get("name") for p in presets if p.get("name")}
    existing_ids = {p.get("model") for p in presets if p.get("model")}

    added = 0
    skipped = 0
    for m in models:
        mid = m["id"]
        if mid in existing_ids:
            skipped += 1
            continue
        # 自动取名：就用模型 id（去掉多余前缀/版本号可做美化，但保持原始 id 最准）
        name = mid
        # 避免重名
        if name in existing_names:
            suffix = 1
            while f"{name}_{suffix}" in existing_names:
                suffix += 1
            name = f"{name}_{suffix}"
        preset = {
            "name": name,
            "mode": "openai",
            "openai_base_url": clean_url,
            "api_key": clean_key,
            "model": mid,
        }
        presets.append(preset)
        existing_names.add(name)
        existing_ids.add(mid)
        added += 1

    if added == 0:
        print(f"\n\033[33m没有新增模型（已有 {len(existing_ids)} 个，全部已存在或被跳过）\033[0m")
        return

    save_model_presets(presets)
    print(f"\n\033[32m✅ 成功导入 {added} 个模型预设！\033[0m")
    if skipped:
        print(f"\033[33m（跳过 {skipped} 个已存在的模型）\033[0m")
    print(f"   当前共 {len(presets)} 个预设")
    print("\033[36m可到「模型配置」菜单中切换使用。\033[0m")

    if input("\n\033[33m是否立即切换到第一个新模型？(y/n, 默认 n)：\033[0m").strip().lower() == "y":
        new_idx = len(presets) - added
        apply_model_preset(new_idx)


def stop_all_proxies(quiet=False):
    """停止所有相关本地代理：扫 /proc 找全部 openai_proxy.py 进程，批量杀"""
    # 先用 /proc 拿全 PID（快速，无需逐端口探活）
    all_pids = _all_openai_proxy_pids()
    stopped = []
    if all_pids:
        _kill_pids(all_pids)
        time.sleep(0.3)
        stopped = all_pids
    # 清理 meta 文件
    presets = load_model_presets()
    for i in range(min(len(presets), MAX_PRESETS)):
        port = PROXY_PORT_BASE + i
        meta_file = proxy_meta_path(port)
        if os.path.exists(meta_file):
            try:
                os.remove(meta_file)
            except Exception:
                pass
    # 默认端口 meta
    for p in [PROXY_PORT]:
        meta_file = proxy_meta_path(p)
        if os.path.exists(meta_file):
            try:
                os.remove(meta_file)
            except Exception:
                pass
    # 确认清理
    orphans = cleanup_orphan_proxies()
    if not quiet:
        if stopped:
            print(f"\033[33m已停止进程 PID: {stopped}\033[0m")
        if orphans:
            print(f"\033[33m已清理残留进程: {orphans}\033[0m")
        else:
            left = _all_openai_proxy_pids()
            if left:
                print(f"\033[31m仍有代理进程: {left}\033[0m")
            else:
                print("\033[32m本地代理已全部离线\033[0m")
    return stopped


def cleanup_orphan_proxies():
    """杀掉所有 openai_proxy.py 残留进程（不在合法端口也杀）"""
    pids = _all_openai_proxy_pids()
    if not pids:
        return []
    _kill_pids(pids)
    time.sleep(0.3)
    return pids

def proxy_status():
    """显示所有代理运行状态"""
    presets = load_model_presets()
    statuses = []
    for i, p in enumerate(presets):
        if p.get("mode") == "openai":
            port = get_preset_port(i)
            if port is None:
                statuses.append((p.get("name", f"预设{i+1}"), None, False))
                continue
            alive = False
            try:
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    alive = r.status == 200
            except Exception:
                pass
            statuses.append((p.get("name", f"预设{i+1}"), port, alive))
    return statuses


def apply_model_preset(index):
    """应用指定索引的预设。

    策略（手机内存友好）：
    - openai：启动当前预设对应的单个本地代理
    - direct：stop_all 本地代理，清 OPENAI_*，纯直连可用
    """
    presets = load_model_presets()
    if not (0 <= index < len(presets)):
        print("\033[31m无效的预设索引\033[0m")
        return False
    preset = presets[index]
    name = preset.get("name", "未知")
    mode = preset.get("mode", "direct")

    if mode == "openai":
        openai_base = preset.get("openai_base_url", "")
        api_key = preset.get("api_key", "")
        model = preset.get("model", "gpt-4o")
        port = get_preset_port(index)
        if port is None:
            print(f"\033[31m预设索引 {index} 超出端口上限 {MAX_PRESETS}\033[0m")
            return False

        if not start_proxy(api_key, openai_base, model, port=port):
            print("\033[31m代理启动失败\033[0m")
            return False

        shezhi(
            f"http://127.0.0.1:{port}", api_key, model,
            extra_env={"OPENAI_API_KEY": api_key, "OPENAI_BASE_URL": openai_base},
        )
        print(f"\033[32m✅ 已切换到预设「{name}」\033[0m")
        print(f"   模式: 代理 → 127.0.0.1:{port} → {openai_base}")
        return True

    # ── 直连：必须可用，不依赖本地代理 ──
    stop_all_proxies(quiet=True)
    url = preset.get("anthropic_base_url", "")
    api_key = preset.get("api_key", "")
    model = preset.get("model", "gpt-4o")
    # extra_env=None → shezhi 清掉 OPENAI_*，避免误判本地代理
    if not shezhi(url, api_key, model, extra_env=None):
        print("\033[31m直连配置写入失败\033[0m")
        return False
    # 再保险：清 OPENAI 残留
    cfg = load_settings()
    env = cfg.get("env", {})
    dirty = False
    for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        if k in env:
            env.pop(k, None)
            dirty = True
    if dirty:
        save_json(PZWJ, cfg)
    print(f"\033[32m✅ 已切换到预设「{name}」\033[0m")
    print(f"   模式: 直连 → {url}")
    print("   本地代理已全部停止（直连不需要）")
    return True


def model_config_menu():
    """
    合并菜单 4+5+7：统一的模型配置管理。
    分两大类：直连模型（Anthropic兼容）、代理模型（OpenAI兼容）。
    每类都可添加多个，一键切换。
    """
    while True:
        presets = load_model_presets()
        direct = [p for p in presets if p.get("mode") != "openai"]
        proxy_list = [p for p in presets if p.get("mode") == "openai"]

        # 当前活跃的模型
        env = load_settings().get("env", {})
        active_url = env.get("ANTHROPIC_BASE_URL", "")
        active_model = env.get("ANTHROPIC_MODEL", "")
        active_name = "无"
        active_port = parse_local_proxy_port(active_url)
        for i, p in enumerate(presets):
            if p.get("mode") == "openai":
                pp = get_preset_port(i)
                if active_port is not None and pp is not None and active_port == pp:
                    active_name = p.get("name", "未知")
                    break
            else:
                u = (p.get("anthropic_base_url") or "").rstrip("/")
                if u and u == active_url.rstrip("/"):
                    active_name = p.get("name", "未知")
                    break

        print(f"\n\033[1;36m══════ 模型配置管理 ({len(presets)}/{MAX_PRESETS}) ══════\033[0m")
        print(f"当前使用: \033[32m{active_name}\033[0m ({active_model})")
        # 显示所有代理状态
        proxy_states = proxy_status()
        alive_count = sum(1 for _, _, a in proxy_states if a)
        if proxy_states:
            print(f"代理状态: \033[32m{alive_count}/{len(proxy_states)} 在线\033[0m")
            for sname, sport, salive in proxy_states:
                icon = "\033[32m●\033[0m" if salive else "\033[31m○\033[0m"
                print(f"  {icon} {sname} (127.0.0.1:{sport})")

        # ── 直连模型 ──
        print(f"\n\033[33m📡 直连模型（Anthropic兼容，无需代理）\033[0m")
        if direct:
            for i, p in enumerate(direct):
                marker = " ◀" if p.get("name") == active_name else ""
                print(f"  \033[32m{i+1}\033[0m. {p.get('name')} → {p.get('model')}{marker}")
        else:
            print("  （暂无，选 a 或 b 添加）")

        # ── 代理模型 ──
        print(f"\n\033[36m🔄 代理模型（OpenAI兼容，需本地代理）\033[0m")
        if proxy_list:
            for i, p in enumerate(proxy_list):
                marker = " ◀" if p.get("name") == active_name else ""
                print(f"  \033[36m{i+1}\033[0m. {p.get('name')} → {p.get('model')}{marker}")
        else:
            print("  （暂无，选 c 添加）")

        print(f"\n\033[1m━━ 操作 ━━\033[0m")
        print("  a. 添加直连模型（DeepSeek/智谱/自定义等）")
        print("  b. 快速预设（一键 DeepSeek/MiMo/智谱/豆包）")
        print("  c. 批量导入 OpenAI 模型（只需地址+Key，自动拉取全部模型）")
        if presets:
            print("  d. 切换模型（应用）")
            print("  e. 删除模型")
        print("  t. 测试连接（检测当前模型 API 是否可用）")
        print("  0. 返回主菜单")
        c = input("\033[1m请选择：\033[0m").strip().lower()

        if c == "0":
            break
        elif c == "a":
            add_model_preset_interactive()
        elif c == "b":
            _quick_presets_submenu()
        elif c == "c":
            _add_proxy_model_interactive()
        elif c == "d" and presets:
            print("\n所有模型：")
            for i, p in enumerate(presets):
                tag = "🔄" if p.get("mode") == "openai" else "🔗"
                print(f"  {i+1}. {tag} {p.get('name')} → {p.get('model')}")
            idx = input("输入要应用的编号：").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(presets):
                apply_model_preset(int(idx) - 1)
        elif c == "e" and presets:
            print("\n所有模型：")
            for i, p in enumerate(presets):
                tag = "🔄" if p.get("mode") == "openai" else "🔗"
                print(f"  {i+1}. {tag} {p.get('name')} → {p.get('model')}")
            idx = input("输入要删除的编号：").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(presets):
                name = presets[int(idx) - 1].get("name", "未知")
                if input(f"\033[31m确认删除「{name}」？(y/n)：\033[0m").strip().lower() == "y":
                    delete_model_preset(int(idx) - 1)
        elif c == "t":
            _test_current_connection()
        else:
            print("\033[31m无效选项\033[0m")
        input("\033[1m回车继续>\033[0m")


def _test_current_connection():
    """测试当前配置的模型是否可用"""
    env = load_settings().get("env", {})
    url = env.get("ANTHROPIC_BASE_URL", "")
    key = env.get("ANTHROPIC_AUTH_TOKEN", "")
    model = env.get("ANTHROPIC_MODEL", "")

    if not url:
        print("\033[31m未配置模型，请先配置\033[0m")
        return

    print(f"\033[33m正在测试连接...\033[0m")
    print(f"  地址: {url}")
    print(f"  模型: {model}")

    if is_local_proxy_mode():
        # 代理模式：先测代理，再测上游
        port = current_proxy_port()
        if not proxy_running(port):
            print(f"\033[31m❌ 代理未运行 (127.0.0.1:{port})，请先菜单 6 重启\033[0m")
            return
        print(f"  \033[32m✅ 代理运行中 (127.0.0.1:{port})\033[0m")
        test_url = f"http://127.0.0.1:{port}/health"
        try:
            import urllib.request
            with urllib.request.urlopen(test_url, timeout=5) as r:
                import json
                data = json.loads(r.read())
                upstream = data.get("upstream", {})
                if upstream.get("reachable"):
                    print(f"  \033[32m✅ 上游可达: {upstream.get('status')}\033[0m")
                else:
                    print(f"  \033[33m⚠️  上游不可达: {upstream.get('error', '未知')}\033[0m")
                    print(f"  \033[33m   请检查 API Key 和 Base URL 是否正确\033[0m")
        except Exception as e:
            print(f"  \033[31m❌ 代理测试失败: {e}\033[0m")
    else:
        # 直连模式：直接请求
        try:
            import urllib.request
            # 用 /v1/models 或简单请求测试
            test_url = _models_endpoint(url)
            req = urllib.request.Request(
                test_url,
                headers={"Authorization": f"Bearer {key}", "User-Agent": "claude-code-test"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"  \033[32m✅ 连接成功 (HTTP {r.status})\033[0m")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print(f"  \033[31m❌ API Key 无效 (401)\033[0m")
            elif e.code == 404:
                print(f"  \033[33m⚠️  接口路径可能不对 (404)，但网络通\033[0m")
            else:
                print(f"  \033[33m⚠️  HTTP {e.code}: {e.reason[:100]}\033[0m")
        except urllib.error.URLError as e:
            print(f"  \033[31m❌ 网络不通: {e.reason}\033[0m")
        except Exception as e:
            print(f"  \033[31m❌ 测试失败: {e}\033[0m")


def _quick_presets_submenu():
    """快速预设子菜单：一键填入 DeepSeek/MiMo/智谱/豆包"""
    print(
        """
\033[1;36m--- 快速预设 ---\033[0m
1.DeepSeek  2.MiMo  3.智谱GLM  4.豆包AI
5.自定义 Anthropic URL
0.返回"""
    )
    b = input("请输入对应数字：").strip()
    if b == "0":
        return
    api = input("API_key：").strip()
    ok_key, api = sanitize_key(api)
    if not ok_key:
        print(f"\033[31m{api}\033[0m")
        return

    if b == "1":
        name = input("预设名称（默认 DeepSeek 写代码）：").strip() or "DeepSeek 写代码"
        model = "deepseek-v4-pro[1m]"
        url = "https://api.deepseek.com/anthropic"
        preset = {"name": name, "mode": "direct", "anthropic_base_url": url, "api_key": api, "model": model}
    elif b == "2":
        name = input("预设名称（默认 MiMo 日常）：").strip() or "MiMo 日常"
        model = "mimo-v2.5-pro[1m]"
        url = "https://api.xiaomimimo.com/anthropic"
        preset = {"name": name, "mode": "direct", "anthropic_base_url": url, "api_key": api, "model": model}
    elif b == "3":
        name = input("预设名称（默认 智谱GLM）：").strip() or "智谱GLM"
        model = "glm-5.2[1m]"
        url = "https://open.bigmodel.cn/api/anthropic"
        preset = {"name": name, "mode": "direct", "anthropic_base_url": url, "api_key": api, "model": model}
    elif b == "4":
        name = input("预设名称（默认 豆包AI）：").strip() or "豆包AI"
        model = "doubao-seed-2.1-pro"
        url = "https://ark.cn-beijing.volces.com/api/compatible"
        preset = {"name": name, "mode": "direct", "anthropic_base_url": url, "api_key": api, "model": model}
    elif b == "5":
        u = input("base_URL：").strip()
        ok, uu = sanitize_url(u, kind="anthropic")
        if not ok:
            print(f"\033[31m{uu}\033[0m")
            return
        name = input("预设名称：").strip() or "自定义"
        model = input("模型名：").strip() or "gpt-4o"
        preset = {"name": name, "mode": "direct", "anthropic_base_url": uu, "api_key": api, "model": model}
    else:
        print("\033[31m无效\033[0m")
        return

    # 保存预设
    presets = load_model_presets()
    replacing = False
    for p in presets:
        if p.get("name") == name:
            if input(f"\033[33m「{name}」已存在，覆盖？(y/n)：\033[0m").strip().lower() != "y":
                return
            presets.remove(p)
            replacing = True
            break
    if not can_add_preset(presets, replacing=replacing):
        print(f"\033[31m预设已达上限 {MAX_PRESETS} 个，请先删除再用\033[0m")
        return
    presets.append(preset)
    save_model_presets(presets)
    print(f"\033[32m预设「{name}」已保存！({len(presets)}/{MAX_PRESETS})\033[0m")

    # 询问是否立即切换
    if input("\033[33m立即切换到该模型？(y/n)：\033[0m").strip().lower() == "y":
        apply_model_preset(len(presets) - 1)


def _add_proxy_model_interactive():
    """添加 OpenAI 代理模型（只需地址+Key，自动拉取模型列表批量导入）。"""
    import_openai_models_interactive()

    # 询问是否立即切换并启动代理
    if input("\033[33m立即切换到该模型并启动代理？(y/n)：\033[0m").strip().lower() == "y":
        presets = load_model_presets()
        if presets:
            apply_model_preset(len(presets) - 1)


# ── 会话管理 ──
SESSIONS_DIR = os.path.join(HOME, ".claude", "projects")


def list_sessions():
    """列出所有历史会话，返回 [(显示名, 路径, 时间戳), ...]"""
    sessions = []
    if not os.path.isdir(SESSIONS_DIR):
        return sessions
    for proj_dir in os.listdir(SESSIONS_DIR):
        proj_path = os.path.join(SESSIONS_DIR, proj_dir)
        if not os.path.isdir(proj_path):
            continue
        for fname in os.listdir(proj_path):
            if fname.endswith(".json"):
                fpath = os.path.join(proj_path, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    title = data.get("title") or data.get("name") or proj_dir
                    updated = data.get("updated_at") or data.get("created_at") or ""
                    sessions.append((title, fpath, updated, proj_dir))
                except Exception:
                    sessions.append((f"{proj_dir}/{fname}", fpath, "", proj_dir))
    # 按时间倒序
    def _sort_key(s):
        ts = s[2]
        if not ts:
            try:
                return os.path.getmtime(s[1])
            except Exception:
                return 0
        return ts
    sessions.sort(key=_sort_key, reverse=True)
    return sessions


def resume_session(session_path):
    """恢复指定会话"""
    if not os.path.exists(session_path):
        print("\033[31m会话文件不存在\033[0m")
        return False
    # Claude Code 支持 --resume 参数，只需要传会话 ID 或文件路径
    print(f"\033[33m正在恢复会话: {os.path.basename(session_path)}\033[0m")
    launch_cc(["--resume", session_path])
    return True


def export_session(session_path):
    """将会话导出为 Markdown 文件"""
    if not os.path.exists(session_path):
        print("\033[31m会话文件不存在\033[0m")
        return
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"\033[31m读取失败: {e}\033[0m")
        return

    # 生成 Markdown
    title = data.get("title") or data.get("name") or "会话导出"
    model = data.get("model") or "unknown"
    lines = [f"# {title}\n", f"> 模型: {model}  |  导出时间: {time.strftime('%Y-%m-%d %H:%M')}\n\n"]

    for msg in data.get("messages") or []:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        parts.append(str(b.get("text", "")))
                    elif b.get("type") == "tool_use":
                        parts.append(f"[tool_use: {b.get('name', '?')}]")
                    elif b.get("type") == "tool_result":
                        parts.append(f"[tool_result]")
                    else:
                        parts.append(str(b))
                else:
                    parts.append(str(b))
            content = "\n".join(parts)
        lines.append(f"### {role}\n\n{content}\n\n")

    export_name = f"claude-session-{int(time.time())}.md"
    export_path = os.path.join(HOME, export_name)
    try:
        with open(export_path, "w", encoding="utf-8") as f:
            f.write("".join(lines))
        print(f"\033[32m已导出: {export_path}\033[0m")
        return export_path
    except Exception as e:
        print(f"\033[31m导出失败: {e}\033[0m")
        return None


def search_conversations(query):
    """搜索历史会话内容"""
    results = []
    sessions_dir = SESSIONS_DIR
    if not os.path.exists(sessions_dir):
        return results
    query_lower = query.lower()
    for proj_dir in os.listdir(sessions_dir):
        proj_path = os.path.join(sessions_dir, proj_dir)
        if not os.path.isdir(proj_path):
            continue
        for fname in os.listdir(proj_path):
            if fname.endswith(".json"):
                fpath = os.path.join(proj_path, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    title = data.get("title") or data.get("name") or proj_dir
                    messages = data.get("messages", [])
                    for msg in messages:
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            parts = []
                            for b in content:
                                if isinstance(b, dict):
                                    parts.append(str(b.get("text", "")))
                                else:
                                    parts.append(str(b))
                            content_str = "\n".join(parts)
                        else:
                            content_str = str(content)
                        if query_lower in content_str.lower():
                            snippet = content_str[:100].replace("\n", " ")
                            results.append((title, fpath, snippet))
                            break
                except Exception:
                    pass
    return results


def backup_sessions():
    """将会话备份到 ~/.claude/backups/"""
    backup_dir = os.path.join(HOME, ".claude", "backups")
    os.makedirs(backup_dir, exist_ok=True)

    sessions = list_sessions()
    count = 0
    for title, fpath, _, proj in sessions:
        try:
            dest = os.path.join(backup_dir, f"{proj}_{os.path.basename(fpath)}")
            with open(fpath, "rb") as src, open(dest, "wb") as dst:
                dst.write(src.read())
            count += 1
        except Exception as e:
            print(f"\033[31m备份失败 {fpath}: {e}\033[0m")

    print(f"\033[32m已备份 {count} 个会话到 {backup_dir}\033[0m")


def delete_session(session_path):
    """删除会话文件"""
    try:
        os.remove(session_path)
        print(f"\033[33m已删除: {os.path.basename(session_path)}\033[0m")
        return True
    except Exception as e:
        print(f"\033[31m删除失败: {e}\033[0m")
        return False


def manage_sessions_menu():
    """会话管理菜单"""
    while True:
        sessions = list_sessions()
        print("\n\033[1;36m--- 会话管理 ---\033[0m")
        if sessions:
            print(f"\033[33m共 {len(sessions)} 个历史会话：\033[0m")
            for i, (title, fpath, updated, proj) in enumerate(sessions[:20]):  # 最多显示20个
                t = updated[:16] if updated else "未知时间"
                short_title = (title[:40] + "..") if len(title) > 40 else title
                print(f"  {i+1:2d}. [{t}] {short_title}")
            if len(sessions) > 20:
                print(f"  ... 还有 {len(sessions)-20} 个")
        else:
            print("  （暂无历史会话，启动过 Claude Code 后会有）")

        print("\n1. 恢复会话（续聊）")
        if sessions:
            print("2. 导出会话为 Markdown")
            print("3. 备份全部会话到存储卡")
            print("4. 删除会话")
        print("5. 搜索会话内容")
        print("0. 返回主菜单")
        c = input("\033[1m请选择：\033[0m").strip()

        if c == "0":
            break
        elif c == "1" and sessions:
            idx = input("输入要恢复的会话编号：").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(sessions):
                _, fpath, _, _ = sessions[int(idx) - 1]
                resume_session(fpath)
        elif c == "2" and sessions:
            idx = input("输入要导出的会话编号：").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(sessions):
                _, fpath, _, _ = sessions[int(idx) - 1]
                export_session(fpath)
        elif c == "3" and sessions:
            backup_sessions()
        elif c == "4" and sessions:
            idx = input("输入要删除的会话编号：").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(sessions):
                _, fpath, _, _ = sessions[int(idx) - 1]
                if input(f"\033[31m确认删除？(y/n)：\033[0m").strip().lower() == "y":
                    delete_session(fpath)
        elif c == "5":
            query = input("搜索关键词：").strip()
            if query:
                results = search_conversations(query)
                if results:
                    print(f"\n找到 {len(results)} 个匹配会话：")
                    for title, path, snippet in results[:10]:
                        print(f"  \033[36m{title}\033[0m")
                        print(f"    {snippet[:80]}...")
                        print()
                else:
                    print("\033[33m无匹配结果\033[0m")
        else:
            print("\033[31m无效选项\033[0m")
        input("\033[1m回车继续>\033[0m")


def shezhi(url, api_key, model, extra_env=None):
    """配置 Anthropic 直连参数"""
    try:
        if url and not is_local_proxy_url(url):
            ok, u = sanitize_url(url, kind="anthropic")
            if not ok:
                print(f"\033[31mURL 非法: {u}\033[0m")
                return False
            url = u
        elif url and is_local_proxy_url(url):
            # 本地代理地址保留原样（支持 18765+ 多端口），只去尾斜杠
            url = url.strip().rstrip("/")
        ok, key = sanitize_key(api_key)
        if not ok:
            print(f"\033[31m{key}\033[0m")
            return False
        model = sanitize_model(model)

        config = load_settings()
        env = config["env"]
        env["ANTHROPIC_BASE_URL"] = url
        env["ANTHROPIC_AUTH_TOKEN"] = key
        env["ANTHROPIC_MODEL"] = model
        env.pop("ANTHROPIC_API_KEY", None)
        for k, v in DEFAULT_LIMITS.items():
            env.setdefault(k, str(v))
        if extra_env:
            for k, v in extra_env.items():
                if v is None:
                    env.pop(k, None)
                else:
                    if k == "OPENAI_BASE_URL":
                        ok2, vv = sanitize_url(str(v), kind="openai")
                        if not ok2:
                            print(f"\033[31mOPENAI_BASE_URL 非法: {vv}\033[0m")
                            return False
                        v = vv
                    if k in ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                        ok2, vv = sanitize_key(str(v))
                        if not ok2:
                            print(f"\033[31m{vv}\033[0m")
                            return False
                        v = vv
                    env[k] = v
        else:
            # 纯直连：清掉 OPENAI_*，避免 launch 误判本地代理 / 脏上游
            if not is_local_proxy_url(url or ""):
                env.pop("OPENAI_API_KEY", None)
                env.pop("OPENAI_BASE_URL", None)
                env.pop("OPENAI_MODEL", None)
        env.pop("ANTHROPIC_API_KEY", None)
        save_json(PZWJ, config)
        print("=======================")
        print("\033[32m配置成功！\033[0m")
        print(f"BASE_URL : {url}")
        print(f"MODEL    : {model}")
        if env.get("OPENAI_BASE_URL"):
            print(f"OPENAI   : {env.get('OPENAI_BASE_URL')}")
        print(
            f"MAX_OUT  : {env.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS', DEFAULT_LIMITS['CLAUDE_CODE_MAX_OUTPUT_TOKENS'])}"
        )
        print("=======================")
        return True
    except Exception as e:
        print("=======================")
        print(f"\033[1;31m配置失败：{e}\033[0m")
        print("=======================")
        return False


def proxy_running(port=None):
    actual_port = port or PROXY_PORT
    try:
        with urllib.request.urlopen(f"http://{PROXY_HOST}:{actual_port}/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def stop_proxy(port=None, quiet=False):
    """停止指定端口的代理；port 为空时停当前 settings 端口（本地代理）或默认 8765。
    优先用 meta 里的 pid；没有 meta 或仍存活时，用 /proc 按监听端口反查 PID 强杀。
    """
    if port is None:
        port = current_proxy_port() if is_local_proxy_mode() else PROXY_PORT
    actual_port = int(port)
    meta_file = proxy_meta_path(actual_port)
    meta = load_json(meta_file, {}) or {}
    pid = meta.get("pid")
    killed = False

    pids = []
    with _proxy_op_lock:
        if pid:
            try:
                pids.append(int(pid))
            except Exception:
                pass
        # 无论有没有 meta，都尝试按端口反查（覆盖 meta 丢失/脏 pid）
        for p in _listening_pids_on_port(actual_port):
            if p not in pids:
                pids.append(p)

        if pids:
            killed = _kill_pids(pids) or killed

        if os.path.exists(meta_file):
            try:
                os.remove(meta_file)
            except Exception:
                pass
    time.sleep(0.3)
    still = proxy_running(actual_port)
    # 仍在线再强杀一轮
    if still:
        with _proxy_op_lock:
            extra = _listening_pids_on_port(actual_port)
            if extra:
                killed = _kill_pids(extra) or killed
                time.sleep(0.2)
                still = proxy_running(actual_port)
    if not quiet:
        if not still:
            print(f"\033[33m本地 OpenAI 代理已停止 (127.0.0.1:{actual_port})\033[0m")
        else:
            print(f"\033[31m未能停止 127.0.0.1:{actual_port}（无权限或非本用户进程）\033[0m")
    return not still


def _trim_log(logpath: str, max_mb: int = 5, keep_lines: int = 2000):
    """日志文件超过 max_mb MB 时，截断保留最后 keep_lines 行。原子写避免与代理写入冲突。"""
    try:
        if not os.path.exists(logpath):
            return
        size_mb = os.path.getsize(logpath) / (1024 * 1024)
        if size_mb < max_mb:
            return
        with open(logpath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) <= keep_lines:
            return
        tmp_path = logpath + ".trim.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(lines[-keep_lines:])
        os.replace(tmp_path, logpath)
    except Exception:
        pass


def start_proxy(openai_key, openai_base, model, port=None, quiet=False):
    if not os.path.exists(PROXY):
        print("\033[1;31m找不到 openai_proxy.py\033[0m")
        print(f"期望路径: {PROXY}")
        return False

    ok, openai_base = sanitize_url(openai_base, kind="openai")
    if not ok:
        print("\033[1;31m拒绝启动代理: OPENAI_BASE_URL 无法解析\033[0m")
        print(openai_base)
        print("可输入: https://token sensenova.cn/v1  （会自动变成 https://token.sensenova.cn/v1）")
        return False
    ok, openai_key = sanitize_key(openai_key)
    if not ok:
        print(f"\033[1;31m{openai_key}\033[0m")
        return False
    model = sanitize_model(model)
    actual_port = int(port) if port is not None else PROXY_PORT
    # 允许：默认 8765，或 18765..18784
    if actual_port != PROXY_PORT and not (PROXY_PORT_BASE <= actual_port <= PROXY_PORT_MAX):
        if not quiet:
            print(f"\033[31m非法代理端口 {actual_port}（允许 {PROXY_PORT} 或 {PROXY_PORT_BASE}-{PROXY_PORT_MAX}）\033[0m")
        return False
    listen_url = f"http://{PROXY_HOST}:{actual_port}"
    meta_file = proxy_meta_path(actual_port)
    log_file = proxy_log_path(actual_port)

    # 已在运行：尽量回填 meta.pid，方便下次 stop
    with _proxy_op_lock:
        if proxy_running(actual_port):
            pids = _listening_pids_on_port(actual_port)
            if pids:
                save_json(
                    meta_file,
                    {
                        "pid": pids[0],
                        "url": listen_url,
                        "port": actual_port,
                        "openai_base_url": openai_base,
                        "model": model,
                        "log": log_file,
                        "key_tail": openai_key[-4:] if len(openai_key) >= 4 else "****",
                    },
                )
            if not quiet:
                print(f"\033[33m代理已在运行 (127.0.0.1:{actual_port}) pid={pids[0] if pids else '?'}\033[0m")
            return True

    # 清理旧 meta，避免脏 pid
    if os.path.exists(meta_file):
        try:
            os.remove(meta_file)
        except Exception:
            pass

    env = os.environ.copy()
    env["OPENAI_API_KEY"] = openai_key
    env["OPENAI_BASE_URL"] = openai_base
    env["OPENAI_MODEL"] = model
    env["PROXY_HOST"] = PROXY_HOST
    env["PROXY_PORT"] = str(actual_port)
    env["PROXY_DEBUG"] = "1"
    env["PROXY_FORCE_NON_STREAM"] = "1"
    env["PROXY_DROP_TOOLS"] = "0"
    # ── 调优参数注入代理子进程（使用户预设自动带上推理/采样增强） ──
    # 代理已支持这些环境变量：OPENAI_REASONING_EFFORT / OPENAI_VERBOSITY /
    # OPENAI_TEMPERATURE / OPENAI_TOP_P / PROXY_HIGH_MAX_TOKENS / PROXY_UPSTREAM_TIMEOUT
    # 若用户在 shell 里已 export，优先使用用户的值；否则给智能默认。
    for key, default in (
        ("OPENAI_REASONING_EFFORT", "high"),       # o系列/gpt-5 推理强度
        ("PROXY_HIGH_MAX_TOKENS", "32000"),          # 大型任务 max_tokens 上限
        ("PROXY_UPSTREAM_TIMEOUT", "180"),           # 推理模型慢，给够时间
        ("OPENAI_PARALLEL_TOOL_CALLS", "1"),         # Claude Code 依赖并行工具
    ):
        env.setdefault(key, default)
    # 以下留空 = 不注入默认值（让上游自行决定），但若用户设了就透传
    for optional_key in (
        "OPENAI_VERBOSITY",
        "OPENAI_TEMPERATURE",
        "OPENAI_TOP_P",
        "OPENAI_FREQUENCY_PENALTY",
        "OPENAI_PRESENCE_PENALTY",
        "OPENAI_SEED",
        "PROXY_REASONING_MODELS",
    ):
        if optional_key in os.environ:
            env.setdefault(optional_key, os.environ[optional_key])

    ensure_dirs()
    _trim_log(log_file, max_mb=5, keep_lines=2000)

    logf = open(log_file, "ab", buffering=0)
    try:
        logf.write(f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} port={actual_port} =====\n".encode())
    except Exception:
        pass
    with _proxy_op_lock:
        proc = subprocess.Popen(
            [sys.executable, "-u", PROXY],
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=DIR,
        )
        logf.close()  # 父进程关闭 fd，子进程已继承
        save_json(
            meta_file,
            {
                "pid": proc.pid,
                "url": listen_url,
                "port": actual_port,
                "openai_base_url": openai_base,
                "model": model,
                "log": log_file,
                "key_tail": openai_key[-4:] if len(openai_key) >= 4 else "****",
            },
        )
    # 健康检查循环在锁外

    for _ in range(40):
        if proc.poll() is not None:
            if not quiet:
                print("\033[1;31m代理进程启动后立即退出\033[0m")
                print(f"查看日志: {log_file}")
                try:
                    print(open(log_file, "r", encoding="utf-8", errors="ignore").read()[-1500:])
                except Exception:
                    pass
            return False
        if proxy_running(actual_port):
            if not quiet:
                print("\033[32m本地 OpenAI 代理已启动\033[0m")
                print(f"地址: {listen_url}")
                print(f"上游: {openai_base}")
                print(f"日志: {log_file}")
            return True
        time.sleep(0.2)

    if not quiet:
        print("\033[1;31m代理健康检查超时\033[0m")
        print(f"期望地址: {listen_url}/health")
        print(f"日志: {log_file}")
        try:
            print(open(log_file, "r", encoding="utf-8", errors="ignore").read()[-1500:])
        except Exception:
            pass
    # 超时仍尝试收尸，避免僵尸进程占端口
    try:
        os.kill(proc.pid, signal.SIGTERM)
    except Exception:
        pass
    return False


def openai_creds_from_settings():
    env = load_settings().get("env", {})
    key = env.get("OPENAI_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN") or ""
    base = env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = env.get("ANTHROPIC_MODEL") or "gpt-4o"
    ok, base2 = sanitize_url(base, kind="openai")
    if ok:
        base = base2
    ok, key2 = sanitize_key(key) if key else (False, key)
    if ok:
        key = key2
    model = sanitize_model(model)
    return key, base, model


def is_local_proxy_mode():
    """检测当前是否配置为本地代理模式（忽略大小写、尾部斜杠）"""
    base = (load_settings().get("env", {}).get("ANTHROPIC_BASE_URL") or "").rstrip("/").lower()
    # 检测所有 127.0.0.1 开头的地址都是代理模式
    if "127.0.0.1" in base or "localhost" in base:
        return True
    proxy_urls = {
        PROXY_URL.rstrip("/").lower(),
        f"http://127.0.0.1:{PROXY_PORT}".rstrip("/").lower(),
        f"http://localhost:{PROXY_PORT}".rstrip("/").lower(),
    }
    return base in proxy_urls


def ensure_proxy_if_needed(force=False):
    if not is_local_proxy_mode() and not force:
        return True
    port = current_proxy_port()
    if proxy_running(port) and not force:
        return True
    key, base, model = openai_creds_from_settings()
    if not key:
        print("\033[1;31mOpenAI 模式缺少 API Key，请先到菜单 4 配置代理模型\033[0m")
        return False
    ok, msg = sanitize_url(base, kind="openai")
    if not ok:
        print("\033[1;31m已保存的 OPENAI_BASE_URL 非法，请重新配置菜单 4\033[0m")
        print(msg)
        return False
    print(f"\033[33m正在启动/重启本地 OpenAI 代理 (127.0.0.1:{port})...\033[0m")
    if force:
        stop_proxy(port=port, quiet=True)
        time.sleep(0.3)
    return start_proxy(key, base, model, port=port)


def restart_proxy_only():
    """菜单 6：重启本地 OpenAI 代理。

    多Agent模式已废弃，只重启当前/唯一代理端口。
    - 非本地代理模式：提示后返回
    """
    if not is_local_proxy_mode():
        print("\033[33m当前不是本地 OpenAI 代理模式\033[0m")
        return

    presets = load_model_presets()
    openai_presets = [(i, p) for i, p in enumerate(presets) if p.get("mode") == "openai"]
    main_port = current_proxy_port()

    # ── 单代理：只重启当前/唯一端口 ──
    actual_port = main_port
    if openai_presets:
        only_idx, only_p = openai_presets[0]
        port_from_preset = get_preset_port(only_idx)
        if port_from_preset is not None:
            actual_port = port_from_preset
        key = only_p.get("api_key") or ""
        base_url = only_p.get("openai_base_url") or ""
        model = only_p.get("model") or "gpt-4o"
    else:
        key, base_url, model = openai_creds_from_settings()

    if not key:
        print("\033[31m缺少 API Key（settings 中无 OPENAI_API_KEY / ANTHROPIC_AUTH_TOKEN）\033[0m")
        return
    print(f"\033[33m重启本地代理 127.0.0.1:{actual_port} ...\033[0m")
    stop_proxy(port=actual_port, quiet=True)
    if proxy_running(actual_port):
        pids = _listening_pids_on_port(actual_port)
        if pids:
            _kill_pids(pids)
        time.sleep(0.4)
    if proxy_running(actual_port):
        print(f"\033[31m旧代理未能释放端口 {actual_port}，启动可能失败\033[0m")
    else:
        time.sleep(0.2)
    if start_proxy(key, base_url, model, port=actual_port):
        meta = load_json(proxy_meta_path(actual_port), {}) or {}
        print(f"\033[32m代理已启动 (127.0.0.1:{actual_port}) pid={meta.get('pid', '?')}\033[0m")
    else:
        print("\033[31m代理启动失败，请看菜单 7 日志\033[0m")


def configure_limits():
    cfg = load_settings()
    env = cfg["env"]
    cur = env.get(
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        DEFAULT_LIMITS["CLAUDE_CODE_MAX_OUTPUT_TOKENS"],
    )
    print(
        f"""
\033[1;36m--- 高级参数 ---\033[0m
当前 CLAUDE_CODE_MAX_OUTPUT_TOKENS = {cur}
1) 64000  2) 100000  3) 128000  4) 自定义  5) 应用默认  0) 返回
"""
    )
    c = input("请选择：").strip()
    if c == "0":
        return
    if c == "5":
        print(apply_default_limits(force=True))
        return
    mapping = {"1": "64000", "2": "100000", "3": "128000"}
    if c in mapping:
        val = mapping[c]
    elif c == "4":
        val = input("数字：").strip()
        if not val.isdigit() or int(val) < 1000:
            print("无效")
            return
    else:
        print("无效")
        return
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = val
    env.pop("ANTHROPIC_API_KEY", None)
    save_json(PZWJ, cfg)
    print(f"\033[32m已写入 CLAUDE_CODE_MAX_OUTPUT_TOKENS={val}\033[0m")


def install_deps():
    """安装依赖：用 subprocess.run 替代 os.system（更安全）"""
    cmds = [
        ["pkg", "update"],
        ["pkg", "upgrade", "-y"],
        ["pkg", "install", "python", "-y"],
        ["pkg", "install", "glibc-repo", "-y"],
        ["pkg", "install", "glibc", "-y"],
    ]
    for cmd in cmds:
        ret = subprocess.run(cmd, capture_output=False)
        if ret.returncode != 0:
            print(f"\033[31m命令执行失败: {' '.join(cmd)}\033[0m")
            input("\033[1m回车继续>\033[0m")
            return

    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("openai_proxy.py", "cc.py", "claude.py", "url_utils.py"):
        src = os.path.join(here, name)
        if os.path.exists(src):
            dst = os.path.join(DIR, name)
            subprocess.run(["cp", "-f", src, dst])
            print(f"\033[32m已同步 {dst}\033[0m")
    apply_default_limits(force=False)


def find_project_root(path: str) -> str:
    """向上查找项目根目录（有 .git / package.json / pyproject.toml 等特征文件）"""
    current = os.path.abspath(path)
    # 最多向上找 5 层
    for _ in range(5):
        for marker in (".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "composer.json"):
            if os.path.exists(os.path.join(current, marker)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return path


def launch_cc(extra_args=None):
    apply_default_limits(force=False)

    # 多Agent模式已废弃，只启动当前需要的单个代理
    if is_local_proxy_mode():
        if not ensure_proxy_if_needed(force=False):
            input("\033[1m回车返回>\033[0m")
            return
        if not proxy_running(current_proxy_port()):
            print(f"\033[1;31m代理仍离线 (127.0.0.1:{current_proxy_port()})，取消启动\033[0m")
            input("\033[1m回车返回>\033[0m")
            return

    cfg = load_settings()
    if "ANTHROPIC_API_KEY" in cfg.get("env", {}):
        cfg["env"].pop("ANTHROPIC_API_KEY", None)
        save_json(PZWJ, cfg)
        print("\033[33m已移除 ANTHROPIC_API_KEY，避免 Auth conflict\033[0m")

    if is_local_proxy_mode():
        ob = cfg.get("env", {}).get("OPENAI_BASE_URL", "")
        ok, msg = sanitize_url(ob, kind="openai")
        if not ok:
            print("\033[1;31mOPENAI_BASE_URL 非法，已阻止启动\033[0m")
            print(msg)
            input("\033[1m回车返回>\033[0m")
            return
        if msg != ob:
            cfg["env"]["OPENAI_BASE_URL"] = msg
            save_json(PZWJ, cfg)

    max_out = cfg.get("env", {}).get(
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        DEFAULT_LIMITS["CLAUDE_CODE_MAX_OUTPUT_TOKENS"],
    )

    # 工作目录：优先检测是否有项目特征文件
    cwd = os.getcwd()
    project_root = find_project_root(cwd)
    if project_root != cwd:
        print(f"\033[33m检测到项目根目录: {project_root}\033[0m")
        workdir = project_root
    elif cwd.rstrip("/") in (DIR.rstrip("/"), os.path.join(DIR, "anzhuang")):
        workdir = HOME
        print(f"\033[33m检测到在安装目录启动，工作目录改为 HOME: {workdir}\033[0m")
    else:
        workdir = cwd

    args = extra_args or []
    cmd = [sys.executable, CC] + args

    # ── 自动降级：如果当前模型不可用，切到备用 ──
    cfg = load_settings()
    active_url = cfg.get("env", {}).get("ANTHROPIC_BASE_URL", "")
    if active_url and "127.0.0.1" not in active_url:
        # 直连模式：测试当前模型
        try:
            test_url = _models_endpoint(active_url)
            key = cfg.get("env", {}).get("ANTHROPIC_AUTH_TOKEN", "")
            import urllib.request
            req = urllib.request.Request(test_url, headers={"Authorization": f"Bearer {key}"}, method="GET")
            with urllib.request.urlopen(req, timeout=5) as r:
                pass  # 模型可用
        except Exception:
            # 当前模型挂了，自动降级
            presets = load_model_presets()
            if len(presets) > 1:
                for i, p in enumerate(presets):
                    if p.get("mode") == "direct" and p.get("anthropic_base_url", "") != active_url:
                        print(f"\033[33m当前模型不可用，自动降级到: {p.get('name')}\033[0m")
                        apply_model_preset(i)
                        break
    elif is_local_proxy_mode() and not proxy_running(current_proxy_port()):
        # 代理挂了，切到另一个代理或直连
        presets = load_model_presets()
        if len(presets) > 1:
            for i, p in enumerate(presets):
                if p.get("mode") != "openai":
                    print(f"\033[33m代理不可用，自动降级到直连: {p.get('name')}\033[0m")
                    apply_model_preset(i)
                    break

    # ── 启动前环境诊断 ──
    issues = []
    print("\n\033[1;36m══════ 环境检查 ══════\033[0m")

    # Python 版本
    print(f"  \033[33mPython\033[0m: {sys.version.split()[0]}")
    print(f"  \033[33m工作目录\033[0m: {workdir}")

    # glibc 链接器
    linker = os.path.join(PREFIX, "glibc", "lib", "ld-linux-aarch64.so.1")
    if os.path.exists(linker):
        print(f"  \033[32m✅ glibc 链接器\033[0m")
    else:
        issues.append("glibc 链接器未安装（请运行菜单 1）")
        print(f"  \033[31m❌ glibc 链接器\033[0m")

    # Claude Code 二进制
    binary = os.path.join(HOME, "Claudecode", "anzhuang", "2.1.159")
    if os.path.exists(binary):
        # 检查 ELF
        try:
            with open(binary, "rb") as f:
                magic = f.read(4)
            if magic == b"\x7fELF":
                print(f"  \033[32m✅ Claude Code 二进制\033[0m")
            else:
                issues.append("Claude Code 二进制文件损坏")
                print(f"  \033[31m❌ Claude Code 二进制格式异常\033[0m")
        except Exception:
            issues.append("无法读取 Claude Code 二进制")
            print(f"  \033[31m❌ Claude Code 二进制不可读\033[0m")
    else:
        issues.append(f"找不到 Claude Code 二进制: {binary}")
        print(f"  \033[31m❌ Claude Code 二进制未找到\033[0m")

    # 模型配置
    cfg_ok = bool(cfg.get("env", {}).get("ANTHROPIC_BASE_URL"))
    cfg_model = cfg.get("env", {}).get("ANTHROPIC_MODEL", "未配置")
    if cfg_ok:
        print(f"  \033[32m✅ 模型配置: {cfg_model}\033[0m")
    else:
        issues.append("未配置模型（请先通过菜单 4 配置）")
        print(f"  \033[31m❌ 模型未配置\033[0m")

    # 代理检查
    if is_local_proxy_mode():
        _pport = current_proxy_port()
        if proxy_running(_pport):
            print(f"  \033[32m✅ 代理运行中 (127.0.0.1:{_pport})\033[0m")
            # 顺便测上游
            try:
                import urllib.request, json
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{_pport}/health", timeout=3
                ) as r:
                    hp = json.loads(r.read())
                    up = hp.get("upstream", {})
                    if up.get("reachable"):
                        print(f"  \033[32m✅ 上游 API 可达\033[0m")
                    else:
                        print(f"  \033[33m⚠️  上游 API 不可达: {up.get('error', '未知')}\033[0m")
                        print(f"     \033[33m请检查 API Key 和 Base URL 是否正确\033[0m")
            except Exception as e:
                print(f"  \033[33m⚠️  代理健康检查失败: {e}\033[0m")
        else:
            issues.append("代理未运行（请通过菜单 4 切换代理模型）")
            print(f"  \033[31m❌ 代理未运行\033[0m")

    if issues:
        print(f"\n  \033[31m发现 {len(issues)} 个问题:\033[0m")
        for i, issue in enumerate(issues, 1):
            print(f"    {i}. {issue}")
        print(f"\n  \033[33m是否仍要启动？(y/n)：\033[0m", end="")
        if input().strip().lower() != "y":
            print("  \033[33m已取消启动\033[0m")
            return

    print("\n\033[1;36m══════ 启动 Claude Code ══════\033[0m")
    _st_port = current_proxy_port() if is_local_proxy_mode() else None
    if _st_port is not None:
        print(f"代理状态: {'在线' if proxy_running(_st_port) else '离线'} (127.0.0.1:{_st_port})")
    else:
        print("代理状态: 非本地/离线")
    print(f"输出上限: {max_out}")
    print(f"工作目录: {workdir}")
    if is_local_proxy_mode():
        print(f"上游URL : {cfg.get('env', {}).get('OPENAI_BASE_URL')}")
        print("\033[36m提示: 读文件/列目录需要 tools；当前代理默认 PROXY_DROP_TOOLS=0\033[0m")

    # ── 代理守护线程（仅代理模式） ──
    # 一个端口就守一个；多个 openai 预设就守多个。凭据优先从 model_presets 取。
    watchdog_stop = threading.Event()
    # port -> 是否已打印过离线提示，避免刷屏
    watchdog_logged_ports = set()

    def _watch_targets():
        """当前应守护的 (port, key, base, model, name) 列表。
        多Agent已废弃：只守护当前激活的单个代理端口。
        """
        targets = []
        if not is_local_proxy_mode():
            return targets
        presets_now = load_model_presets()
        openai_now = [(i, p) for i, p in enumerate(presets_now) if p.get("mode") == "openai"]
        if openai_now:
            i, p = openai_now[0]
            port = get_preset_port(i) or current_proxy_port()
            targets.append((
                port,
                p.get("api_key") or "",
                p.get("openai_base_url") or "",
                p.get("model") or "gpt-4o",
                p.get("name") or f"port{port}",
            ))
        else:
            key, base, model = openai_creds_from_settings()
            targets.append((current_proxy_port(), key, base, model, "main"))
        return targets

    def _proxy_watchdog():
        """代理守护：每 15 秒检查应守护的全部端口，挂了按预设凭据自动重启。"""
        while not watchdog_stop.is_set():
            if watchdog_stop.wait(15):
                break
            if not is_local_proxy_mode():
                continue
            try:
                targets = _watch_targets()
            except Exception:
                continue
            alive_ports = set()
            for port, key, base, model, name in targets:
                try:
                    if proxy_running(port):
                        alive_ports.add(port)
                        watchdog_logged_ports.discard(port)
                        continue
                    if port not in watchdog_logged_ports:
                        print(f"\033[33m[守护] 代理离线 {name} (127.0.0.1:{port})，自动重启...\033[0m")
                        watchdog_logged_ports.add(port)
                    if key and base:
                        start_proxy(key, base, model, port=port, quiet=True)
                except Exception:
                    pass
            # 清理已不在目标列表里的日志标记
            target_ports = {t[0] for t in targets}
            for p in list(watchdog_logged_ports):
                if p not in target_ports:
                    watchdog_logged_ports.discard(p)

    if is_local_proxy_mode():
        wd = threading.Thread(target=_proxy_watchdog, daemon=True)
        wd.start()
        n_watch = len(_watch_targets())
        print(f"\033[32m  代理守护已启动（每 15 秒检查 {n_watch} 个端口）\033[0m")

    try:
        ret = subprocess.run(cmd, cwd=workdir)
    except KeyboardInterrupt:
        ret = None
        pass
    finally:
        watchdog_stop.set()

    # ── 退出后菜单：继续/换模型/回主菜单 ──
    print("\n\033[1;36m=== Claude Code 已退出 ===\033[0m")
    while True:
        print("1. 用当前模型重新启动（可从历史会话恢复）")
        print("2. 换个模型继续（选预设，保留上下文）")
        print("0. 回主菜单")
        choice = input("\033[1m请选择：\033[0m").strip()

        if choice == "0":
            break
        elif choice == "1":
            # 用当前配置重新启动
            args = extra_args or []
            cmd2 = [sys.executable, CC] + args
            try:
                ret = subprocess.run(cmd2, cwd=workdir)
            except KeyboardInterrupt:
                pass
            continue
        elif choice == "2":
            # 选预设换模型
            presets = load_model_presets()
            if not presets:
                print("\033[33m暂无预设，请先到主菜单 7 添加预设\033[0m")
                continue
            print("\n\033[33m选择要切换的模型预设：\033[0m")
            for i, p in enumerate(presets):
                mode_tag = "🔄代理" if p.get("mode") == "openai" else "🔗直连"
                print(f"  {i+1}. {mode_tag} {p.get('name')} → {p.get('model')}")
            idx = input("请输入编号（0 取消）：").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(presets):
                apply_model_preset(int(idx) - 1)
                # 重新启动
                args = extra_args or []
                cmd2 = [sys.executable, CC] + args
                try:
                    ret = subprocess.run(cmd2, cwd=workdir)
                except KeyboardInterrupt:
                    pass
            continue
        else:
            print("\033[31m无效选项\033[0m")
            continue


def show_log():
    port = current_proxy_port() if is_local_proxy_mode() else PROXY_PORT
    log_path = proxy_log_path(port)
    print(f"==== {log_path} (port {port}) ====")
    print("  小哲")
    if not os.path.exists(log_path):
        # 回退显示默认日志
        if log_path != PROXY_LOG and os.path.exists(PROXY_LOG):
            print(f"(当前端口无日志，显示默认) {PROXY_LOG}")
            log_path = PROXY_LOG
        else:
            print("(无日志)")
            return
    try:
        print(open(log_path, "r", encoding="utf-8", errors="ignore").read()[-3000:])
    except Exception as e:
        print(e)


def status_line():
    env = load_settings().get("env", {})
    model = env.get("ANTHROPIC_MODEL") or "无模型配置"
    base = env.get("ANTHROPIC_BASE_URL") or ""
    max_out = env.get(
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        DEFAULT_LIMITS["CLAUDE_CODE_MAX_OUTPUT_TOKENS"],
    )
    oai = env.get("OPENAI_BASE_URL") or ""
    oai_state = ""
    if oai:
        ok, _ = sanitize_url(oai, kind="openai")
        oai_state = "OK" if ok else "非法!"
    if "127.0.0.1" in base or "localhost" in base:
        _sp = parse_local_proxy_port(base) or PROXY_PORT
        mode = f"OpenAI(本地代理:{_sp}) / " + (
            "\033[32m在线\033[0m" if proxy_running(_sp) else "\033[31m离线\033[0m"
        )
    elif base:
        mode = "Anthropic兼容直连"
    else:
        mode = "未配置"
    return model, mode, base, max_out, oai, oai_state


def batch_test_presets():
    """批量测试所有预设模型"""
    presets = load_model_presets()
    if not presets:
        print("\n\033[33m暂无预设，请先添加\033[0m")
        return
    
    print("\n\033[1;36m══════ 批量测试所有预设 ══════\033[0m\n")
    
    results = []
    for i, p in enumerate(presets):
        name = p.get("name", f"预设{i+1}")
        mode = p.get("mode", "direct")
        model = p.get("model", "unknown")
        
        if mode == "openai":
            url = p.get("openai_base_url", "")
            port = get_preset_port(i)
            if port is None:
                results.append((name, mode, model, False, False, 0))
                continue
            proxy_ok = proxy_running(port)
            api_ok = False
            latency = 0
            if proxy_ok:
                try:
                    import urllib.request, json, time
                    test_url = f"http://127.0.0.1:{port}/health"
                    start = time.time()
                    with urllib.request.urlopen(test_url, timeout=5) as r:
                        data = json.loads(r.read())
                        latency = round((time.time() - start) * 1000)
                        upstream = data.get("upstream", {})
                        api_ok = upstream.get("reachable", False)
                except Exception:
                    pass
            results.append((name, mode, model, proxy_ok, api_ok, latency))
        else:
            api_key = p.get("api_key", "")
            base_url = p.get("anthropic_base_url", "")
            api_ok = False
            latency = 0
            try:
                import urllib.request, time, urllib.error
                test_url = _models_endpoint(base_url)
                req = urllib.request.Request(test_url, headers={"Authorization": f"Bearer {api_key}"}, method="GET")
                start = time.time()
                with urllib.request.urlopen(req, timeout=10) as r:
                    latency = round((time.time() - start) * 1000)
                    api_ok = r.status == 200
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    api_ok = False
                    latency = 0
            except Exception:
                pass
            results.append((name, mode, model, True, api_ok, latency))
    
    print(f"{'预设名称':20s} {'模式':6s} {'模型':25s} {'状态':8s} {'延迟':8s}")
    print("-" * 70)
    for name, mode, model, _, api_ok, latency in results:
        status = "\033[32m✅\033[0m" if api_ok else "\033[31m❌\033[0m"
        lat_str = f"{latency}ms" if latency > 0 else "-"
        short_name = name[:18] if len(name) > 18 else name
        print(f"{short_name:20s} {mode:6s} {model[:24]:25s} {status:8s} {lat_str:8s}")
    
    ok = sum(1 for _, _, _, _, api_ok, _ in results if api_ok)
    print(f"\n结果: {ok}/{len(results)} 可用")


# ── 新增：配置导入/导出 ──

def export_config():
    """导出配置到存储卡"""
    EXPORT_DIR = os.path.join(HOME, "storage", "downloads", "claude_config_backup")
    os.makedirs(EXPORT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    files = {
        "settings.json": PZWJ,
        "model_presets.json": MODEL_PRESETS_FILE,
        "failover_presets.json": _FAILOVER_PRESETS_FILE,
        "usage_stats.json": os.path.join(os.path.dirname(PZWJ), "usage_stats.json"),
    }
    exported = []
    for name, src in files.items():
        if os.path.exists(src):
            dst = os.path.join(EXPORT_DIR, f"{ts}_{name}")
            try:
                shutil.copy2(src, dst)
                exported.append(name)
            except Exception as e:
                print(f"\033[31m导出 {name} 失败: {e}\033[0m")
    if exported:
        print(f"\033[32m✅ 已导出 {len(exported)} 个文件到:\033[0m")
        print(f"   {EXPORT_DIR}/")
        for n in exported:
            print(f"   - {ts}_{n}")
    else:
        print("\033[33m没有可导出的配置\033[0m")


def import_config():
    """从存储卡导入配置"""
    IMPORT_DIR = os.path.join(HOME, "storage", "downloads", "claude_config_backup")
    if not os.path.isdir(IMPORT_DIR):
        print(f"\033[31m导入目录不存在: {IMPORT_DIR}\033[0m")
        return
    backups = sorted([f for f in os.listdir(IMPORT_DIR) if f.endswith(".json")])
    if not backups:
        print("\033[33m没有找到备份文件\033[0m")
        return
    print("\n\033[1;36m可导入的备份文件：\033[0m")
    for i, f in enumerate(backups):
        fpath = os.path.join(IMPORT_DIR, f)
        size = os.path.getsize(fpath)
        print(f"  {i+1}. {f} ({size} bytes)")
    idx = input("\n输入编号导入（0 取消）：").strip()
    if not idx.isdigit() or int(idx) < 1 or int(idx) > len(backups):
        return
    selected = backups[int(idx) - 1]
    fpath = os.path.join(IMPORT_DIR, selected)
    try:
        # 自动识别文件类型并导入到对应位置
        name_map = {
            "settings.json": PZWJ,
            "model_presets.json": MODEL_PRESETS_FILE,
            "failover_presets.json": _FAILOVER_PRESETS_FILE,
            "usage_stats.json": os.path.join(os.path.dirname(PZWJ), "usage_stats.json"),
        }
        imported = False
        for key, dst in name_map.items():
            if key in selected:
                shutil.copy2(fpath, dst)
                print(f"\033[32m✅ 已导入 {key}\033[0m")
                imported = True
                break
        if not imported:
            print(f"\033[33m跳过: 无法识别文件类型 {selected}\033[0m")
    except Exception as e:
        print(f"\033[31m导入失败: {e}\033[0m")


def backup_config():
    """备份当前配置到备份目录"""
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    files = {
        "settings.json": PZWJ,
        "model_presets.json": MODEL_PRESETS_FILE,
        "failover_presets.json": _FAILOVER_PRESETS_FILE,
        "usage_stats.json": os.path.join(os.path.dirname(PZWJ), "usage_stats.json"),
    }
    count = 0
    for name, src in files.items():
        if os.path.exists(src):
            dst = os.path.join(_BACKUP_DIR, f"{ts}_{name}")
            try:
                shutil.copy2(src, dst)
                count += 1
            except Exception:
                pass
    print(f"\033[32m已备份 {count} 个配置到 {_BACKUP_DIR}\033[0m")


# ── 新增：预设搜索/过滤 ──

def search_presets():
    """搜索预设（按名称/模型/URL）"""
    presets = load_model_presets()
    if not presets:
        print("\033[33m暂无预设\033[0m")
        return
    query = input("搜索关键词（名称/模型/URL，回车=显示全部）：").strip().lower()
    results = []
    for i, p in enumerate(presets):
        name = (p.get("name") or "").lower()
        model = (p.get("model") or "").lower()
        url = (p.get("openai_base_url") or p.get("anthropic_base_url") or "").lower()
        if not query or query in name or query in model or query in url:
            results.append((i, p))
    if not results:
        print(f"\033[33m没有匹配「{query}」的预设\033[0m")
        return
    print(f"\n\033[1;36m找到 {len(results)} 个匹配预设：\033[0m")
    for i, p in results:
        mode_tag = "🔄" if p.get("mode") == "openai" else "🔗"
        name = p.get("name", "未知")
        model = p.get("model", "?")
        url = (p.get("openai_base_url") or p.get("anthropic_base_url") or "")[:50]
        print(f"  {i+1}. {mode_tag} {name} → {model}")
        if url:
            print(f"     {url}...")
    input("\n\033[1m回车继续>\033[0m")


# ── 新增：配置热重载检测 ──

def check_presets_reload() -> bool:
    """检查 model_presets.json 是否有变更，有则自动重载"""
    global _MODEL_PRESETS_MTIME
    try:
        mtime = os.path.getmtime(MODEL_PRESETS_FILE)
        if mtime > _MODEL_PRESETS_MTIME:
            _MODEL_PRESETS_MTIME = mtime
            return True
    except Exception:
        pass
    return False


def get_presets_summary() -> str:
    """获取预设数量摘要（用于状态栏显示）"""
    presets = load_model_presets()
    direct = sum(1 for p in presets if p.get("mode") != "openai")
    proxy = sum(1 for p in presets if p.get("mode") == "openai")
    return f"{len(presets)}个预设({direct}直连/{proxy}代理)"


def check_and_update():
    """检查更新并一键更新，不覆盖配置"""
    print("\n\033[1;36m══════ 检查更新 ══════\033[0m")
    
    # GitHub 仓库地址（通过环境变量配置）
    REPO = os.environ.get("CC_UPDATE_REPO", "")
    if not REPO:
        print("\033[33m未配置更新源。请设置环境变量 CC_UPDATE_REPO 为你的 GitHub raw 仓库地址。\033[0m")
        print("\033[33m例如: export CC_UPDATE_REPO=https://raw.githubusercontent.com/用户名/仓库名/main\033[0m")
        return
    FILES = ["url_utils.py", "cc.py", "claude.py", "openai_proxy.py"]
    
    # 获取当前版本（从文件头部）
    import urllib.request, json
    
    # 尝试获取 SHA256 校验文件
    checksums = {}
    try:
        req_cs = urllib.request.Request(f"{REPO}/SHA256.txt", headers={"User-Agent": "claude-code-updater"})
        with urllib.request.urlopen(req_cs, timeout=10) as r:
            for line in r.read().decode("utf-8").strip().split("\n"):
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    checksums[parts[1].strip()] = parts[0].strip()
    except Exception:
        pass
    
    # 检查每个文件的更新
    updates_found = False
    for fname in FILES:
        local_path = os.path.join(DIR, fname)
        remote_url = f"{REPO}/{fname}"
        
        try:
            # 获取远程文件
            req = urllib.request.Request(remote_url, headers={"User-Agent": "claude-code-updater"})
            with urllib.request.urlopen(req, timeout=10) as r:
                remote_content = r.read().decode("utf-8")
            
            if not remote_content:
                print(f"  \033[33m{fname}: 远程文件为空，跳过\033[0m")
                continue
            
            # SHA256 校验
            fname_key = fname
            if fname_key in checksums:
                import hashlib
                actual = hashlib.sha256(remote_content.encode("utf-8")).hexdigest()
                if actual != checksums[fname_key]:
                    print(f"  \033[31m{fname}: SHA256 校验失败，跳过\033[0m")
                    continue
            
            # 检查本地文件是否存在
            if os.path.exists(local_path):
                with open(local_path, "r", encoding="utf-8") as f:
                    local_content = f.read()
                
                if local_content == remote_content:
                    print(f"  \033[32m{fname}: 已是最新\033[0m")
                    continue
            
            # 备份旧文件
            if os.path.exists(local_path):
                backup_path = local_path + ".bak"
                import shutil
                shutil.copy2(local_path, backup_path)
            
            # 写入新文件
            with open(local_path, "w", encoding="utf-8") as f:
                f.write(remote_content)
            print(f"  \033[32m✅ {fname}: 已更新\033[0m")
            updates_found = True
            
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  \033[33m{fname}: 远程不存在，跳过\033[0m")
            else:
                print(f"  \033[31m{fname}: 下载失败 (HTTP {e.code})\033[0m")
        except Exception as e:
            print(f"  \033[31m{fname}: 出错 - {e}\033[0m")
    
    if updates_found:
        print("\n\033[32m✅ 更新完成！配置文件和数据不受影响\033[0m")
        print("\033[33m提示: 如果运行异常，备份文件在 .bak 结尾\033[0m")
    else:
        print("\n\033[33m所有文件已是最新\033[0m")


def show_token_stats():
    """显示 token 用量统计仪表盘"""
    STATS_FILE = os.path.join(os.path.dirname(PZWJ), "usage_stats.json")
    print("\n\033[1;36m══════ Token 用量统计 ══════\033[0m")

    if not os.path.exists(STATS_FILE):
        print("  小哲\n")
        print("\n  \033[33m暂无数据，使用代理后会自动记录\033[0m")
        print("  \033[33m提示：只有通过 OpenAI 代理模式才会记录用量\033[0m")
        return

    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            stats = json.load(f)
    except Exception as e:
        print(f"\n  \033[31m读取失败: {e}\033[0m")
        return

    if not isinstance(stats, dict) or not stats.get("models"):
        print("\n  \033[33m暂无数据\033[0m")
        return

    total = stats.get("total", {})
    pt = total.get("prompt_tokens", 0)
    ct = total.get("completion_tokens", 0)
    cost = total.get("cost_usd", 0.0)

    # 总计
    print(f"\n  \033[1m总计\033[0m")
    print(f"  ├─ 输入: {pt:,} tokens")
    print(f"  ├─ 输出: {ct:,} tokens")
    print(f"  ├─ 合计: {pt+ct:,} tokens")
    print(f"  └─ 费用: \033[33m${cost:.4f}\033[0m (约 ¥{cost*7.3:.2f})")

    # 按模型
    models = stats.get("models", {})
    print(f"\n  \033[1m按模型\033[0m")
    for name, m in sorted(models.items(), key=lambda x: x[1].get("cost_usd", 0), reverse=True):
        mp = m.get("prompt_tokens", 0)
        mc = m.get("completion_tokens", 0)
        mcst = m.get("cost_usd", 0)
        last = m.get("last_used", "")[:10]
        print(f"  ├─ \033[36m{name}\033[0m")
        print(f"  │  输入: {mp:,}  输出: {mc:,}  费用: \033[33m${mcst:.4f}\033[0m  最近: {last}")

    # 按天
    daily = stats.get("daily", {})
    if daily:
        print(f"\n  \033[1m最近 7 天\033[0m")
        days = sorted(daily.items(), reverse=True)[:7]
        for day, d in days:
            dp = d.get("prompt_tokens", 0)
            dc = d.get("completion_tokens", 0)
            dcst = d.get("cost_usd", 0)
            cnt = d.get("count", 0)
            print(f"  ├─ \033[33m{day}\033[0m  {cnt}次  {dp+dc:,} tokens  \033[33m${dcst:.4f}\033[0m")

    print()


CC_VERSION = "2.2.0"
UPDATE_URL = "https://raw.githubusercontent.com/你的用户名/claude-code-termux/main"

def main():
    # 确保 ~/.claude/settings.json 权限正确
    if os.path.exists(PZWJ):
        try:
            os.chmod(PZWJ, 0o600)
        except Exception:
            pass

    apply_default_limits(force=False)
    # 启动时自动备份配置
    backup_config()
    # 启动时检查更新提示
    _auto_check_update()
    while True:
        os.system("clear")
        model, mode, base, max_out, oai, oai_state = status_line()
        presets_summary = get_presets_summary()
        print(
            f"""\033[38;5;208m
 ██████╗██╗      █████╗ ██╗   ██╗██████╗ ███████╗
██╔════╝██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝
██║     ██║     ███████║██║   ██║██║  ██║█████╗
╚██████╗███████╗██║  ██║╚██████╔╝██████╔╝███████╗
 ╚═════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝╚══════╝
\033[0m
\033[38;5;214m╔═══════════════════════════════════╗
║         Claude code v2.1.159      ║
╚═══════════════════════════════════╝\033[0m
              小哲
当前模型：{model}
当前模式：{mode}
预设概况：{presets_summary}
BASE_URL：{base or '无'}
上游OPENAI：{oai or '无'} {('['+oai_state+']') if oai_state else ''}
输出上限：{max_out}
1.安装依赖/同步脚本
\033[32m2.普通启动\033[0m
\033[31m3.全权限启动\033[0m
\033[33m4.模型配置管理（直连+代理+预设）\033[0m
5.停止本地OpenAI代理
\033[36m6.重启本地OpenAI代理\033[0m
7.查看代理日志
\033[35m8.高级参数(输出token上限)\033[0m
\033[36m9.用量统计\033[0m
\033[34m10.会话管理\033[0m
\033[35m11.批量测试所有预设\033[0m
\033[36m12.检查更新\033[0m
\033[33m13.配置导入/导出/备份\033[0m
\033[36m14.搜索预设\033[0m
0.退出"""
        )
        a = input("\033[1m请选择对应数字：\033[0m").strip()
        if a == "1":
            install_deps()
        elif a == "2":
            launch_cc()
        elif a == "3":
            print("==========================================================")
            print("\033[1;4;31m☢️ 将跳过所有权限申请☢️\033[0m")
            print("==========================================================")
            launch_cc(["--permission-mode", "bypassPermissions"])
        elif a == "4":
            model_config_menu()
        elif a == "5":
            # 一键停干净：当前 + 全部预设端口 + 8765 + 孤儿
            stop_all_proxies(quiet=False)
        elif a == "6":
            restart_proxy_only()
        elif a == "7":
            show_log()
        elif a == "8":
            configure_limits()
        elif a == "9":
            show_token_stats()
        elif a == "10":
            manage_sessions_menu()
        elif a == "11":
            batch_test_presets()
        elif a == "12":
            check_and_update()
        elif a == "13":
            _config_management_menu()
        elif a == "14":
            search_presets()
        elif a == "0":
            sys.exit(0)
        else:
            print("\033[41;30m仅支持菜单数字\033[0m")
        input("\033[1m回车继续>\033[0m")


def _config_management_menu():
    """配置导入/导出/备份子菜单"""
    while True:
        os.system("clear")
        print("\033[1;36m══════ 配置管理 ══════\033[0m")
        print("1. 备份配置到备份目录")
        print("2. 导出配置到存储卡")
        print("3. 从存储卡导入配置")
        print("0. 返回主菜单")
        c = input("\033[1m请选择：\033[0m").strip()
        if c == "0":
            break
        elif c == "1":
            backup_config()
        elif c == "2":
            export_config()
        elif c == "3":
            import_config()
        else:
            print("\033[31m无效选项\033[0m")
        input("\033[1m回车继续>\033[0m")


def _auto_check_update():
    """启动时静默检查更新提示"""
    REPO = os.environ.get("CC_UPDATE_REPO", "")
    if not REPO:
        return
    try:
        import urllib.request
        # 只检查版本号，不下载
        version_url = f"{REPO}/VERSION"
        req = urllib.request.Request(version_url, method="GET", headers={"User-Agent": "claude-code-updater"})
        with urllib.request.urlopen(req, timeout=3) as r:
            remote_ver = r.read().decode("utf-8", errors="ignore").strip()
        if remote_ver and remote_ver != CC_VERSION:
            print(f"\033[33m💡 发现新版本 {remote_ver}（当前 {CC_VERSION}），请到菜单 12 检查更新\033[0m")
            time.sleep(1)
    except Exception:
        pass


if __name__ == "__main__":
    main()