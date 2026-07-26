<!-- markdownlint-disable MD033 -->
<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8%2B-blue?logo=python&amp;logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/platform-Termux%2FAndroid-orange?logo=android&amp;logoColor=white" alt="Platform">
  <img src="https://img.shields.io/github/stars/xiaozhe7772222/claude-code-termux-proxy?style=flat&amp;logo=github" alt="Stars">
  <img src="https://img.shields.io/github/last-commit/xiaozhe7772222/claude-code-termux-proxy?style=flat" alt="Last Commit">
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen" alt="PRs Welcome">
</p>

# Termux Claude Code OpenAI Proxy

> **唯一为 Termux/Android 优化的 Claude Code OpenAI 协议转换代理**  
> 在手机上跑 Claude Code，连接国产/第三方 API，自带自动故障转移、熔断器、重试机制。

---

## 为什么需要这个？

大多数 AI 中转站只提供 **OpenAI 协议**。为应对日渐增长的 token 消耗和 AI 服务需求，在此开创了 **OpenAI 协议转换代理** @xiaozhe

这个代理做的是：

```
Claude Code 客户端
    ↓ 发 Anthropic 格式请求
openai_proxy.py（本地 127.0.0.1:8765）
    ↓ 转发 OpenAI 格式请求
AI 中转站 / 第三方 API（DeepSeek/智谱/豆包/OpenAI 等）
```

---

## 功能特性

### 核心代理
- ✅ **Anthropic ↔ OpenAI 协议转换** — 消息、工具调用、流式 SSE 完整转换
- ✅ **指数退避重试** — 3 次重试（1s→2s→4s），429/5xx/网络超时自动重试
- ✅ **熔断器** — 连续失败超阈值（默认 5 次）自动熔断 30s，避免浪费请求
- ✅ **Retry-After 解析** — 尊重服务端限流等待时间
- ✅ **连接池** — 复用 TCP 连接，减少握手开销
- ✅ **请求追踪 ID** — 每个请求带 trace_id，日志可追溯
- ✅ **推理参数注入** — 所有模型自动注入 `reasoning_effort=high`
- ✅ **大型任务增强** — 自动检测大型任务，拉高 max_tokens + 强化 system prompt
- ✅ **Token 用量统计** — 按模型/按天统计，自动计算费用
- ✅ **优雅关闭** — SIGTERM/SIGINT 信号处理，保存统计数据

### Termux 专属
- ✅ **glibc 启动器** — `cc.py` 自动检测 Termux glibc 环境，启动 Claude Code 二进制
- ✅ **多版本自动检测** — 自动扫描 `anzhuang/` 目录取最新版本
- ✅ **termux-wake-lock** — 启动时获取 WakeLock，防止后台被杀
- ✅ **环境变量清理** — 自动清理 `LD_*`、`ANTHROPIC_API_KEY`，避免冲突
- ✅ **日志文件** — 启动日志输出到 `~/.claude/cc.log`

### 管理工具
- ✅ **全功能菜单界面** — 14 个菜单项，一键管理
- ✅ **模型预设管理** — 支持 200 个预设，直连/代理模式一键切换
- ✅ **批量导入模型** — 输入地址+Key，自动拉取 `/v1/models` 批量建预设
- ✅ **预设搜索** — 按名称/模型/URL 搜索过滤
- ✅ **配置导入/导出** — 备份到存储卡，换手机也能恢复
- ✅ **启动自动备份** — 每次启动自动备份配置到 `~/.claude/backups/`
- ✅ **批量测试** — 一键测试所有预设的可用性
- ✅ **会话管理** — 列表/恢复/导出/搜索/备份历史会话
- ✅ **自动更新检查** — 启动时静默检查远程版本
- ✅ **配置热重载检测** — 预设文件变更后自动重载

---

## 安装

### 前提条件

- Termux 环境（[下载 Termux](https://f-droid.org/packages/com.termux/)）
- Python 3.8+
- Claude Code 二进制（[官方发布页](https://docs.anthropic.com/en/docs/claude-code/overview)）

### 一键安装

```bash
# 下载项目
git clone https://github.com/xiaozhe7772222/claude-code-termux-proxy.git
cd claude-code-termux-proxy

# 一键安装
bash install.sh
```

安装脚本会自动完成：
1. 安装依赖（`python`、`glibc`）
2. 创建 `~/Claudecode/` 目录
3. 复制脚本文件
4. 初始化配置

### 手动安装

```bash
# 安装依赖
pkg install python glibc-repo glibc

# 把项目文件直接转移到存储卡目录
# 用 MT 管理器 / 文件管理器 将文件复制到:
# /storage/BA73-022B/Claudecode/

# 启动
python /storage/BA73-022B/Claudecode/claude.py
```

---

## 快速开始

```bash
# 启动菜单
python ~/Claudecode/claude.py
```

菜单界面：

```
┌─────────────────────────────────────────┐
│         Claude code v2.2.0              │
├─────────────────────────────────────────┤
│ 当前模型：DeepSeek 写代码                │
│ 当前模式：Anthropic兼容直连              │
│ 预设概况：5个预设(3直连/2代理)           │
│ 1.安装依赖/同步脚本                      │
│ 2.普通启动                              │
│ 3.全权限启动                            │
│ 4.模型配置管理                          │
│ ...                                     │
│ 0.退出                                  │
└─────────────────────────────────────────┘
```

**首次使用步骤：**

1. 选 **1** 安装依赖
2. 选 **4** → **a** 添加直连模型（如 DeepSeek）
3. 输入 API Key 和模型名
4. 选 **2** 启动 Claude Code

---

## 项目结构

```
claude-code-termux-proxy/
├── openai_proxy.py      # 核心代理服务（Anthropic→OpenAI 协议转换）
├── claude.py            # 主管理菜单脚本
├── cc.py                # Claude Code 启动器（glibc + 环境清理）
├── url_utils.py         # URL/Key 清洗公共模块
├── install.sh           # 一键安装脚本
├── test_proxy.py        # 单元测试（38 个用例）
├── LICENSE              # MIT 许可证
├── .gitignore           # Git 忽略规则
└── README.md            # 本文件
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | 上游 API 地址 |
| `OPENAI_API_KEY` | — | 上游 API Key |
| `PROXY_PORT` | `8765` | 本地代理端口 |
| `PROXY_DEBUG` | `1` | 调试日志开关 |
| `PROXY_MAX_RETRIES` | `3` | 最大重试次数 |
| `PROXY_CIRCUIT_THRESHOLD` | `5` | 熔断阈值 |
| `PROXY_CIRCUIT_COOLDOWN` | `30` | 熔断恢复时间（秒） |
| `PROXY_BACKOFF_BASE` | `1.0` | 退避基数（秒） |
| `PROXY_UPSTREAM_TIMEOUT` | `180` | 上游请求超时（秒） |
| `CC_MAX_PRESETS` | `200` | 预设数量上限 |

---

## 与竞品对比

| 功能 | 本项目 | 1rgs/claude-code-proxy (3.7k⭐) |
|------|--------|--------------------------------|
| **Termux/Android 适配** | ✅ 原生支持 | ❌ 桌面端专用 |
| **glibc 启动器** | ✅ `cc.py` 自动检测 | ❌ |
| **termux-wake-lock** | ✅ 防止后台被杀 | ❌ |
| **指数退避重试** | ✅ 3次+429/5xx/超时 | ❌ 一次失败就报错 |
| **熔断器** | ✅ 连续失败自动熔断 | ❌ |
| **连接池** | ✅ 复用 TCP 连接 | ❌ |
| **请求追踪 ID** | ✅ trace_id 贯穿日志 | ❌ |
| **配置导入/导出** | ✅ 到存储卡 | ❌ |
| **Token 用量统计** | ✅ 按模型/按天统计 | ❌ |
| **批量测试预设** | ✅ | ❌ |
| **会话管理** | ✅ 列表/恢复/导出/搜索 | ❌ |

---

## 测试

```bash
python test_proxy.py
```

运行 38 个单元测试，覆盖：
- URL 清洗（8 个用例）
- API Key 清洗（6 个用例）
- 模型名清洗（5 个用例）
- 协议转换核心逻辑（18 个用例）
- 模块可导入性（1 个用例）

---

## 许可证

[MIT License](LICENSE)

## 作者

小哲

## 致谢

- [Anthropic Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)
- [1rgs/claude-code-proxy](https://github.com/1rgs/claude-code-proxy) — 桌面端参考实现

---

<br>
<br>
<br>

---

# English Version

---

# Termux Claude Code OpenAI Proxy

> **The only Claude Code OpenAI protocol proxy optimized for Termux/Android**  
> Run Claude Code on your phone, connect to any OpenAI-compatible API, with built-in auto failover, circuit breaker, and retry mechanism.

---

## Why This?

Most AI relay services only provide **OpenAI protocol**. To handle growing token consumption and AI service demands, this **OpenAI protocol proxy** was created. @xiaozhe

What this proxy does:

```
Claude Code Client
    ↓ sends Anthropic format requests
openai_proxy.py (localhost 127.0.0.1:8765)
    ↓ forwards OpenAI format requests
AI Relay / Third-party API (DeepSeek/GLM/Doubao/OpenAI etc.)
```

---

## Features

### Core Proxy
- ✅ **Anthropic ↔ OpenAI Protocol Conversion** — Messages, tool calls, streaming SSE fully converted
- ✅ **Exponential Backoff Retry** — 3 retries (1s→2s→4s), auto-retry on 429/5xx/network timeout
- ✅ **Circuit Breaker** — Auto-opens for 30s after consecutive failures (default 5), prevents wasted requests
- ✅ **Retry-After Parsing** — Respects server-side rate limiting wait times
- ✅ **Connection Pool** — Reuses TCP connections, reduces handshake overhead
- ✅ **Request Trace ID** — Each request has a trace_id for full log traceability
- ✅ **Reasoning Boost** — Injects `reasoning_effort=high` for all models
- ✅ **Large Task Enhancement** — Auto-detects complex tasks, boosts max_tokens + strengthens system prompt
- ✅ **Token Usage Stats** — Per-model and daily statistics with cost calculation
- ✅ **Graceful Shutdown** — SIGTERM/SIGINT signal handling, saves stats data

### Termux Exclusive
- ✅ **glibc Launcher** — `cc.py` auto-detects Termux glibc environment, launches Claude Code binary
- ✅ **Multi-version Auto Detection** — Scans `anzhuang/` directory for latest version
- ✅ **termux-wake-lock** — Acquires WakeLock on startup to prevent background kill
- ✅ **Environment Variable Cleanup** — Auto-cleans `LD_*` and `ANTHROPIC_API_KEY` to avoid conflicts
- ✅ **Log Files** — Startup logs written to `~/.claude/cc.log`

### Management Tools
- ✅ **Full Menu Interface** — 14 menu items, one-key management
- ✅ **Model Preset Management** — Supports 200 presets, one-click switch between direct/proxy modes
- ✅ **Batch Model Import** — Enter URL+Key, auto-fetch `/v1/models` and create presets in bulk
- ✅ **Preset Search** — Filter by name/model/URL keywords
- ✅ **Config Export/Import** — Backup to SD card, restore on a new phone
- ✅ **Auto Backup on Startup** — Automatically backs up config to `~/.claude/backups/`
- ✅ **Batch Test** — One-click test all presets' availability
- ✅ **Session Management** — List/resume/export/search/backup historical sessions
- ✅ **Auto Update Check** — Silently checks remote version on startup
- ✅ **Config Hot Reload** — Auto-detects preset file changes

---

## Installation

### Prerequisites

- Termux environment ([Download Termux](https://f-droid.org/packages/com.termux/))
- Python 3.8+
- Claude Code binary ([Official page](https://docs.anthropic.com/en/docs/claude-code/overview))

### One-click Install

```bash
# Download the project
git clone https://github.com/xiaozhe7772222/claude-code-termux-proxy.git
cd claude-code-termux-proxy

# One-click install
bash install.sh
```

The install script will:
1. Install dependencies (`python`, `glibc`)
2. Create `~/Claudecode/` directory
3. Copy script files
4. Initialize configuration

### Manual Install

```bash
# Install dependencies
pkg install python glibc-repo glibc

# Transfer project files to SD card directory
# Use MT Manager / File Manager to copy files to:
# /storage/BA73-022B/Claudecode/

# Launch
python /storage/BA73-022B/Claudecode/claude.py
```

---

## Quick Start

```bash
# Launch the menu
python ~/Claudecode/claude.py
```

Menu interface:

```
┌─────────────────────────────────────────┐
│         Claude code v2.2.0              │
├─────────────────────────────────────────┤
│ Current Model: DeepSeek                 │
│ Current Mode: Anthropic Direct          │
│ Presets: 5 (3 direct / 2 proxy)         │
│ 1. Install Dependencies                 │
│ 2. Normal Launch                        │
│ 3. Full Permission Launch               │
│ 4. Model Configuration                  │
│ ...                                     │
│ 0. Exit                                 │
└─────────────────────────────────────────┘
```

**First-time steps:**

1. Select **1** to install dependencies
2. Select **4** → **a** to add a direct model (e.g. DeepSeek)
3. Enter API Key and model name
4. Select **2** to launch Claude Code

---

## Project Structure

```
claude-code-termux-proxy/
├── openai_proxy.py      # Core proxy (Anthropic→OpenAI protocol conversion)
├── claude.py            # Main management menu script
├── cc.py                # Claude Code launcher (glibc + env cleanup)
├── url_utils.py         # URL/Key sanitization module
├── install.sh           # One-click install script
├── test_proxy.py        # Unit tests (38 test cases)
├── LICENSE              # MIT License
├── .gitignore           # Git ignore rules
├── README.md            # Documentation (Chinese + English)
└── README_EN.md         # English documentation (standalone)
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Upstream API URL |
| `OPENAI_API_KEY` | — | Upstream API Key |
| `PROXY_PORT` | `8765` | Local proxy port |
| `PROXY_DEBUG` | `1` | Debug logging toggle |
| `PROXY_MAX_RETRIES` | `3` | Max retry count |
| `PROXY_CIRCUIT_THRESHOLD` | `5` | Circuit breaker threshold |
| `PROXY_CIRCUIT_COOLDOWN` | `30` | Circuit breaker cooldown (seconds) |
| `PROXY_BACKOFF_BASE` | `1.0` | Backoff base (seconds) |
| `PROXY_UPSTREAM_TIMEOUT` | `180` | Upstream request timeout (seconds) |
| `CC_MAX_PRESETS` | `200` | Max preset count |

---

## Comparison with Competitors

| Feature | This Project | 1rgs/claude-code-proxy (3.7k⭐) |
|---------|-------------|--------------------------------|
| **Termux/Android Support** | ✅ Native | ❌ Desktop only |
| **glibc Launcher** | ✅ `cc.py` auto-detect | ❌ |
| **termux-wake-lock** | ✅ Prevents background kill | ❌ |
| **Exponential Backoff Retry** | ✅ 3 times + 429/5xx/timeout | ❌ Fails on first error |
| **Circuit Breaker** | ✅ Auto-open on failures | ❌ |
| **Connection Pool** | ✅ Reuses TCP connections | ❌ |
| **Request Trace ID** | ✅ trace_id in all logs | ❌ |
| **Config Export/Import** | ✅ To SD card | ❌ |
| **Token Usage Stats** | ✅ Per model/per day | ❌ |
| **Batch Test Presets** | ✅ | ❌ |
| **Session Management** | ✅ List/resume/export/search | ❌ |

---

## Testing

```bash
python test_proxy.py
```

Runs 38 unit tests, covering:
- URL sanitization (8 cases)
- API Key sanitization (6 cases)
- Model name sanitization (5 cases)
- Protocol conversion core logic (18 cases)
- Module importability (1 case)

---

## License

[MIT License](LICENSE)

## Author

小哲 (@xiaozhe)

## Acknowledgements

- [Anthropic Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)
- [1rgs/claude-code-proxy](https://github.com/1rgs/claude-code-proxy) — Desktop reference implementation