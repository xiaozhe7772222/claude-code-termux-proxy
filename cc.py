#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==========================================================
# @xiaozhe - Termux Claude Code OpenAI Proxy
# 版权所有 © 2026 小哲
# ==========================================================
"""
用 Termux glibc 动态链接器启动 Claude Code 二进制。
不污染子进程 LD_*；配置来自 ~/.claude/settings.json。
支持多版本检测、日志重定向、termux-wake-lock。
"""
import os
import sys
import subprocess
import glob
import logging

HOME = os.environ.get("HOME", "/data/data/com.termux/files/home")
PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
GLIBC_LIB = os.path.join(PREFIX, "glibc", "lib")
LD_LINKER = os.path.join(GLIBC_LIB, "ld-linux-aarch64.so.1")
INSTALL_DIR = os.path.join(HOME, "Claudecode")
LOG_FILE = os.path.join(HOME, ".claude", "cc.log")


def is_elf_file(path: str) -> bool:
    """检查文件是否为有效的 ELF 二进制"""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        return magic == b"\x7fELF"
    except Exception:
        return False


def find_cc_binary() -> str:
    """查找 Claude Code 二进制，支持多版本目录"""
    # 优先使用 anzhuang 目录下的最新版本
    anzhuang_dir = os.path.join(INSTALL_DIR, "anzhuang")
    if os.path.isdir(anzhuang_dir):
        versions = sorted(glob.glob(os.path.join(anzhuang_dir, "*")), reverse=True)
        for v in versions:
            binary = os.path.join(v, "Claude")
            if os.path.isfile(binary) and is_elf_file(binary):
                return binary
            binary = os.path.join(v, "claude")
            if os.path.isfile(binary) and is_elf_file(binary):
                return binary
    # 回退到默认路径
    default = os.path.join(INSTALL_DIR, "anzhuang", "2.1.159")
    if os.path.isfile(default):
        return default
    return ""


def setup_logging():
    """配置日志：同时输出到 stderr 和文件"""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logger = logging.getLogger("cc_launcher")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%m-%d %H:%M:%S")
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger


def acquire_wake_lock():
    """尝试获取 Termux WakeLock"""
    try:
        subprocess.run(["termux-wake-lock"], check=False, capture_output=True, timeout=5)
    except Exception:
        pass


def release_wake_lock():
    """尝试释放 Termux WakeLock"""
    try:
        subprocess.run(["termux-wake-unlock"], check=False, capture_output=True, timeout=5)
    except Exception:
        pass


def main():
    logger = setup_logging()
    errors = []

    if not os.path.exists(LD_LINKER):
        errors.append(
            f"找不到动态链接器: {LD_LINKER}\n"
            "    请先安装: pkg install glibc-repo && pkg install glibc"
        )

    cc_binary = find_cc_binary()
    if not cc_binary:
        errors.append(f"找不到 Claude Code 二进制: {INSTALL_DIR}/anzhuang/")
    elif not is_elf_file(cc_binary):
        errors.append(f"文件不是有效的 ELF 格式: {cc_binary}")

    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)

    logger.info(f"启动 Claude Code: {cc_binary}")
    acquire_wake_lock()

    clean_env = os.environ.copy()
    clean_env.pop("LD_LIBRARY_PATH", None)
    clean_env.pop("LD_PRELOAD", None)
    clean_env["HOME"] = HOME
    clean_env.pop("ANTHROPIC_API_KEY", None)
    clean_env.setdefault("PWD", os.getcwd())
    clean_env.setdefault("TERMUX_VERSION", clean_env.get("TERMUX_VERSION", "1"))

    cmd = [
        LD_LINKER,
        "--library-path",
        GLIBC_LIB,
        cc_binary,
    ] + sys.argv[1:]

    try:
        ret = subprocess.call(cmd, env=clean_env, cwd=os.getcwd())
        raise SystemExit(ret)
    except KeyboardInterrupt:
        logger.info("用户中断")
        print("\n[!] Exited.")
        raise SystemExit(130)
    except FileNotFoundError as e:
        logger.error(f"启动失败: {e}")
        sys.exit(1)
    finally:
        release_wake_lock()


if __name__ == "__main__":
    main()