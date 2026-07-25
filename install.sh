#!/data/data/com.termux/files/usr/bin/bash
# ==========================================================
# @xiaozhe - Termux Claude Code OpenAI Proxy
# 版权所有 © 2026 小哲
# 用法：bash install.sh
# ==========================================================

set -e

RED='\033[31m'
GREEN='\033[32m'
YELLOW='\033[33m'
CYAN='\033[36m'
NC='\033[0m'

HOME_DIR="${HOME:-/data/data/com.termux/files/home}"
INSTALL_DIR="$HOME_DIR/Claudecode"
CLAUDE_DIR="$HOME_DIR/.claude"

echo -e "${CYAN}"
echo "╔════════════════════════════════════════╗"
echo "║  Termux Claude Code OpenAI Proxy 安装  ║"
echo "╚════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. 检查 Termux 环境 ──
echo -e "${YELLOW}[1/5] 检查 Termux 环境...${NC}"
if [ ! -d "$PREFIX" ]; then
    echo -e "${RED}✗ 未检测到 Termux 环境，请用 Termux 运行此脚本${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Termux 环境正常${NC}"

# ── 2. 安装依赖 ──
echo -e "${YELLOW}[2/5] 安装依赖（pkg update/upgrade/python/glibc）...${NC}"
pkg update -y 2>&1 || true
echo -e "${YELLOW}  ⚠ 跳过全量升级以保护现有环境${NC}"
pkg install -y python glibc-repo glibc 2>&1 || {
    echo -e "${YELLOW}⚠ 批量安装失败，尝试逐个安装...${NC}"
    echo -e "${YELLOW}⚠ 部分依赖安装需重试，尝试单独安装...${NC}"
    pkg install -y python || echo -e "${RED}⚠ python 安装失败，请手动执行: pkg install python${NC}"
    pkg install -y glibc-repo || echo -e "${RED}⚠ glibc-repo 安装失败，请手动执行: pkg install glibc-repo${NC}"
    pkg install -y glibc || echo -e "${RED}⚠ glibc 安装失败，请手动执行: pkg install glibc${NC}"
}
echo -e "${GREEN}✓ 依赖安装完成${NC}"

# ── 3. 创建目录 ──
echo -e "${YELLOW}[3/5] 创建目录...${NC}"
mkdir -p "$INSTALL_DIR"
mkdir -p "$CLAUDE_DIR"
echo -e "${GREEN}✓ 目录已创建: $INSTALL_DIR${NC}"

# ── 4. 复制脚本 ──
echo -e "${YELLOW}[4/5] 安装脚本文件...${NC}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FILES=("openai_proxy.py" "cc.py" "claude.py" "url_utils.py" "test_proxy.py")
COPIED=0
for f in "${FILES[@]}"; do
    SRC="$SCRIPT_DIR/$f"
    if [ -f "$SRC" ]; then
        cp "$SRC" "$INSTALL_DIR/$f"
        chmod 644 "$INSTALL_DIR/$f"
        echo -e "${GREEN}  ✓ $f${NC}"
        COPIED=$((COPIED + 1))
    else
        echo -e "${YELLOW}  ⚠ 未找到 $f，跳过${NC}"
    fi
done

if [ $COPIED -eq 0 ]; then
    echo -e "${RED}✗ 没有复制任何文件，请确认脚本与 install.sh 在同一目录${NC}"
    exit 1
fi
echo -e "${GREEN}✓ 已安装 $COPIED 个脚本文件到 $INSTALL_DIR${NC}"

# ── 5. 初始化配置 ──
echo -e "${YELLOW}[5/5] 初始化默认配置...${NC}"
SETTINGS="$CLAUDE_DIR/settings.json"
if [ ! -f "$SETTINGS" ]; then
    cat > "$SETTINGS" << 'EOF'
{
  "env": {
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"
  }
}
EOF
    chmod 600 "$SETTINGS"
    echo -e "${GREEN}✓ 已创建默认配置文件${NC}"
else
    echo -e "${GREEN}✓ 配置文件已存在，跳过${NC}"
fi

# ── 完成 ──
echo ""
echo -e "${GREEN}╔════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║          安装完成！                     ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
echo -e "启动菜单: ${CYAN}python $INSTALL_DIR/claude.py${NC}"
echo ""
echo -e "首次使用建议:"
echo -e "  1. ${YELLOW}python $INSTALL_DIR/claude.py${NC} 启动菜单"
echo -e "  2. 选 ${YELLOW}4${NC} 进入模型配置管理"
echo -e "  3. 选 ${YELLOW}a${NC} 添加直连模型 或 ${YELLOW}c${NC} 导入 OpenAI 模型"
echo -e "  4. 选 ${YELLOW}2${NC} 启动 Claude Code"
echo ""
echo -e "需要 Claude Code 二进制？请自行放置到:"
echo -e "  ${CYAN}$INSTALL_DIR/anzhuang/版本号/Claude${NC}"
echo ""
echo ""