#!/usr/bin/env bash
# ==============================================================================
# agy-argon-bridge 一键安装与配置脚本
# ==============================================================================
set -e

GREEN="\033[32m"
BLUE="\033[34m"
YELLOW="\033[33m"
RED="\033[31m"
BOLD="\033[1m"
RESET="\033[0m"

echo -e "${BLUE}${BOLD}======================================================${RESET}"
echo -e "${BLUE}${BOLD}  ✨ 安装 Antigravity CLI -> Gemini 4 Argon 本地桥接器  ${RESET}"
echo -e "${BLUE}${BOLD}======================================================${RESET}"

# 1. 检查 Python3
PYTHON_BIN=""
if [ -f "$HOME/miniconda3/bin/python3" ]; then
    PYTHON_BIN="$HOME/miniconda3/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo -e "${RED}[ERROR] 未检测到 python3，请先安装 Python 3.10+。${RESET}"
    exit 1
fi
echo -e "${GREEN}✓ 使用 Python: ${PYTHON_BIN}${RESET}"

# 2. 检查依赖
echo -e "${YELLOW}正在检查并安装 Python 依赖库 (fastapi, uvicorn, google-genai)...${RESET}"
"$PYTHON_BIN" -m pip install -q -r "$(dirname "$0")/requirements.txt" || true
echo -e "${GREEN}✓ Python 依赖就绪${RESET}"

# 3. 检查 gcloud ADC
ADC_PATH="$HOME/.config/gcloud/application_default_credentials.json"
if [ ! -f "$ADC_PATH" ]; then
    echo -e "${YELLOW}[!] 未检测到 Google Cloud 默认应用凭据 (ADC)。${RESET}"
    echo -e "正在拉起 gcloud 浏览器授权登录（以挂载 300$ 赠金）..."
    gcloud auth application-default login
fi
echo -e "${GREEN}✓ Google Cloud ADC 凭据已存在: ${ADC_PATH}${RESET}"

# 4. 拷贝文件到持久化安装路径
INSTALL_DIR="$HOME/.local/share/agy-argon"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SCRIPT_DIR/gemini_argon_bridge.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/bin/agy-argon" "$BIN_DIR/"
chmod +x "$BIN_DIR/agy-argon"
echo -e "${GREEN}✓ 桥接核心及启动入口已安装至 ${INSTALL_DIR} 和 ${BIN_DIR}${RESET}"

# 5. macOS Launchd 后台常驻自启服务配置
if [[ "$OSTYPE" == "darwin"* ]]; then
    PLIST_DEST="$HOME/Library/LaunchAgents/com.antigravity.argon-bridge.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    
    # 替换模板变量
    sed -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
        -e "s|{{HOME}}|$HOME|g" \
        "$SCRIPT_DIR/launchd/com.antigravity.argon-bridge.plist.template" > "$PLIST_DEST"
        
    echo -e "${YELLOW}正在重载 launchd 开机自启服务...${RESET}"
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    launchctl load "$PLIST_DEST"
    echo -e "${GREEN}✓ macOS launchd 后台服务已成功载入常驻！${RESET}"
fi

# 6. 配置 Shell Alias
RC_FILE=""
if [ -n "$ZSH_VERSION" ] || [ -f "$HOME/.zshrc" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    RC_FILE="$HOME/.bashrc"
fi

if [ -n "$RC_FILE" ]; then
    if ! grep -q "alias agy=" "$RC_FILE" 2>/dev/null; then
        echo "" >> "$RC_FILE"
        echo "# Antigravity -> Gemini 4 Argon Bridge Alias" >> "$RC_FILE"
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC_FILE"
        echo 'alias agy="agy-argon"' >> "$RC_FILE"
        echo -e "${GREEN}✓ 已向 ${RC_FILE} 追加 alias agy=\"agy-argon\"${RESET}"
    else
        echo -e "${GREEN}✓ ${RC_FILE} 中已存在 alias 配置${RESET}"
    fi
fi

# 7. 健康检查
sleep 1.2
echo -e "${YELLOW}正在执行健康检查 (http://127.0.0.1:8050/healthz)...${RESET}"
if curl -s http://127.0.0.1:8050/healthz | grep -q "gemini-4-argon"; then
    echo -e "${GREEN}${BOLD}🎉 接入成功！本地桥接器已在 8050 端口完美运行。${RESET}"
    echo -e "${BLUE}提示：执行 'source ${RC_FILE}' 或重启终端后，输入 'agy' 即可享受 Gemini 4 Argon！${RESET}"
else
    echo -e "${RED}[WARN] 端口 8050 暂未响应，可查看日志：${INSTALL_DIR}/bridge.stderr.log${RESET}"
fi
