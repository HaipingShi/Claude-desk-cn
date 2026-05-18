#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
LOG_DIR="$SCRIPT_DIR/Logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +"%Y%m%d-%H%M%S")"
COMMAND_LOG="$LOG_DIR/command-$STAMP.log"
LATEST_COMMAND_LOG="$LOG_DIR/command-latest.log"
touch "$COMMAND_LOG" "$LATEST_COMMAND_LOG"
exec > >(tee "$COMMAND_LOG" "$LATEST_COMMAND_LOG") 2>&1

echo "命令日志: $LATEST_COMMAND_LOG"
echo "开始时间: $(date "+%Y-%m-%d %H:%M:%S")"
echo

echo "修复 Claude Code 运行时配置..."
echo "这会退出 Claude，终止旧 Code 子进程，并把当前第三方推理配置同步到 ~/.claude/settings.json。"
echo "不会修改 /Applications/Claude.app，也不会替换应用。"
echo "如果 Code 报 401，只需要运行这个脚本。"
echo

/usr/bin/python3 patch_claude_zh_cn.py --repair-code-runtime --app /Applications/Claude.app --user-home "$HOME" "$@"

echo
echo "修复日志：$SCRIPT_DIR/Logs/latest.json"
echo "命令日志：$SCRIPT_DIR/Logs/command-latest.log"
echo "完成后请重新打开 Claude，再进入 Code 模式测试。"
echo
read -r -p "按回车关闭窗口..."
