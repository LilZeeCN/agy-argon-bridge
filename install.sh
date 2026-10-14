#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/.local/share/agy-argon"
BIN_DIR="$HOME/.local/bin"
PYTHON_BIN="$(command -v python3)"
PORT="${ARGON_BRIDGE_PORT:-8050}"
LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"

command -v agy >/dev/null || { echo "请先安装原版 Antigravity CLI (agy)。" >&2; exit 1; }
mkdir -p "$INSTALL_DIR" "$BIN_DIR"

# 依赖只安装到桥接器自己的环境；安装失败立即停止。
"$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
BRIDGE_PYTHON="$INSTALL_DIR/venv/bin/python"
"$BRIDGE_PYTHON" -m pip install -q -r "$SCRIPT_DIR/requirements.txt"

ADC_PATH="$HOME/.config/gcloud/application_default_credentials.json"
if [ ! -f "$ADC_PATH" ] && [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
    command -v gcloud >/dev/null || { echo "缺少 ADC，请先安装 gcloud 并登录。" >&2; exit 1; }
    gcloud auth application-default login
fi

PROJECT_ID="$(PYTHONPATH="$SCRIPT_DIR" "$BRIDGE_PYTHON" -c 'from gemini_argon_bridge import PROJECT_ID; print(PROJECT_ID)')"
printf 'export GOOGLE_CLOUD_PROJECT=%q\nexport GOOGLE_CLOUD_LOCATION=%q\nexport ARGON_BRIDGE_PORT=%q\n' \
    "$PROJECT_ID" "$LOCATION" "$PORT" > "$INSTALL_DIR/config.env"
if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
    printf 'export GOOGLE_APPLICATION_CREDENTIALS=%q\n' "$GOOGLE_APPLICATION_CREDENTIALS" >> "$INSTALL_DIR/config.env"
fi
cp "$SCRIPT_DIR/gemini_argon_bridge.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/bin/verify_argon.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/bin/agy-argon" "$BIN_DIR/"
chmod +x "$BIN_DIR/agy-argon"

if [[ "$OSTYPE" == "darwin"* ]]; then
    # plistlib 避免路径中的 XML 字符破坏配置，并显式保存运行所需环境。
    "$BRIDGE_PYTHON" - "$SCRIPT_DIR" "$INSTALL_DIR" "$PROJECT_ID" "$LOCATION" "$PORT" <<'PY'
import os, pathlib, plistlib, sys
source, install, project, location, port = sys.argv[1:]
template = pathlib.Path(source, 'launchd/com.antigravity.argon-bridge.plist.template')
with template.open('rb') as f:
    config = plistlib.load(f)
config['ProgramArguments'] = [str(pathlib.Path(install, 'venv/bin/python')), str(pathlib.Path(install, 'gemini_argon_bridge.py'))]
config['WorkingDirectory'] = install
config['StandardOutPath'] = str(pathlib.Path(install, 'bridge.stdout.log'))
config['StandardErrorPath'] = str(pathlib.Path(install, 'bridge.stderr.log'))
config['EnvironmentVariables'] = {'GOOGLE_CLOUD_PROJECT': project, 'GOOGLE_CLOUD_LOCATION': location, 'ARGON_BRIDGE_PORT': port, 'PYTHONUNBUFFERED': '1'}
if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
    config['EnvironmentVariables']['GOOGLE_APPLICATION_CREDENTIALS'] = os.environ['GOOGLE_APPLICATION_CREDENTIALS']
dest = pathlib.Path.home() / 'Library/LaunchAgents/com.antigravity.argon-bridge.plist'
dest.parent.mkdir(parents=True, exist_ok=True)
with dest.open('wb') as f:
    plistlib.dump(config, f)
PY
    PLIST_DEST="$HOME/Library/LaunchAgents/com.antigravity.argon-bridge.plist"
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    launchctl load "$PLIST_DEST"
fi

RC_FILE=""
if [ -f "$HOME/.zshrc" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    RC_FILE="$HOME/.bashrc"
fi
if [ -n "$RC_FILE" ] && ! grep -q 'alias agy=' "$RC_FILE"; then
    printf '\n# Antigravity -> Vertex AI Argon Bridge\nexport PATH="$HOME/.local/bin:$PATH"\nalias agy="agy-argon"\n' >> "$RC_FILE"
fi

# 启动入口会等待健康接口，失败时不输出接入成功。
"$BIN_DIR/agy-argon" --version
echo "安装完成。文本后端：gemini-4-argon；项目：${PROJECT_ID}；端口：${PORT}。"
echo "真实云端模型可用验证命令核对；赠金抵扣请查看 Cloud Billing 明细。"
