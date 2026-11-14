#!/bin/bash
set -euo pipefail

UID_VALUE="$(id -u)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
for label in \
  com.xianyu-auto.orchestrator \
  com.xianyu-auto.backup
do
  launchctl bootout "gui/$UID_VALUE/$label" 2>/dev/null || true
  rm -f "$AGENTS_DIR/$label.plist"
done

echo "XianyuAuto 常驻任务已停止。数据、素材、数据库和备份均未删除。"
