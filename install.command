#!/bin/bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/usr/bin/python3"
LOG_DIR="$DIR/Logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +"%Y%m%d-%H%M%S")"
COMMAND_LOG="$LOG_DIR/command-$STAMP.log"
LATEST_COMMAND_LOG="$LOG_DIR/command-latest.log"
touch "$COMMAND_LOG" "$LATEST_COMMAND_LOG"
exec > >(tee "$COMMAND_LOG" "$LATEST_COMMAND_LOG") 2>&1

echo "命令日志: $LATEST_COMMAND_LOG"
echo "开始时间: $(date "+%Y-%m-%d %H:%M:%S")"
echo

if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3)"
fi

echo "Claude Desktop 中文补丁"
echo "目录: $DIR"
echo

if [ "$(id -u)" -ne 0 ]; then
  echo "需要管理员权限来替换 /Applications/Claude.app。"
  echo "请按提示输入这台 Mac 的登录密码。"
  echo
  sudo "$PYTHON" "$DIR/patch_claude_zh_cn.py" --user-home "$HOME" --launch "$@"
  STATUS=$?
  echo
  echo "按回车退出。"
  read -r _
  exit "$STATUS"
fi

USER_HOME="$HOME"
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  USER_HOME="/Users/$SUDO_USER"
fi

"$PYTHON" "$DIR/patch_claude_zh_cn.py" --user-home "$USER_HOME" --launch "$@"

echo
echo "完成。按回车退出。"
read -r _
