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
- ✅ **Reasoning Model Support** — Auto-detects o1/o3/gpt-5 etc., injects `reasoning_effort=high`
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
- ✅ **Quick Preset Templates** — One-click fill for DeepSeek/MiMo/GLM/Doubao
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
├── README.md            # Chinese documentation
└── README_EN.md         # English documentation (this file)
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