#!/usr/bin/env bash
# ==============================================================================
# agy-argon-bridge 一键卸载与恢复脚本
# ==============================================================================
set -e

GREEN="\033[32m"
YELLOW="\033[33m"
BOLD="\033[1m"
RESET="\033[0m"

echo -e "${YELLOW}${BOLD}正在卸载 agy-argon-bridge 服务...${RESET}"

# 1. 停止 launchd
PLIST_DEST="$HOME/Library/LaunchAgents/com.antigravity.argon-bridge.plist"
if [ -f "$PLIST_DEST" ]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    echo -e "${GREEN}✓ 已停止并清理 launchd 自启服务${RESET}"
fi

# 2. 清理安装目录与可执行文件
rm -rf "$HOME/.local/share/agy-argon"
rm -f "$HOME/.local/bin/agy-argon"
echo -e "${GREEN}✓ 已删除 ~/.local/share/agy-argon 与 ~/.local/bin/agy-argon${RESET}"

# 3. 提示清理 rc 文件
echo -e "${YELLOW}请注意：若需彻底还原，请在 ~/.zshrc 或 ~/.bashrc 中手动移除 'alias agy=\"agy-argon\"'。${RESET}"
echo -e "${GREEN}${BOLD}卸载完成！${RESET}"
