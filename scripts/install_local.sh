#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UID_VALUE="$(id -u)"
RUNTIME_HOME="${XIANYU_AUTO_HOME:-$HOME/Library/Application Support/XianyuAuto}"
RUNTIME_VENV="$RUNTIME_HOME/venv"
AGENTS_DIR="$HOME/Library/LaunchAgents"
ORCH_LABEL="com.xianyu-auto.orchestrator"
BACKUP_LABEL="com.xianyu-auto.backup"
ORCH_PLIST="$AGENTS_DIR/$ORCH_LABEL.plist"
BACKUP_PLIST="$AGENTS_DIR/$BACKUP_LABEL.plist"

mkdir -p "$RUNTIME_HOME/logs" "$RUNTIME_HOME/bin" "$AGENTS_DIR"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv 未安装。请先安装 uv，再重新运行本脚本。" >&2
  exit 1
fi

uv sync --python 3.12
uv venv --clear --python 3.12 "$RUNTIME_VENV"
uv pip install --python "$RUNTIME_VENV/bin/python" "$ROOT"
"$RUNTIME_VENV/bin/playwright" install chromium
cp "$ROOT/scripts/backup_local.py" "$RUNTIME_HOME/bin/backup_local.py"
chmod 700 "$RUNTIME_HOME/bin/backup_local.py"
"$RUNTIME_VENV/bin/xianyu-auto" init

launchctl bootout "gui/$UID_VALUE/$ORCH_LABEL" 2>/dev/null || true
launchctl bootout "gui/$UID_VALUE/$BACKUP_LABEL" 2>/dev/null || true
bootstrap_agent() {
  local plist="$1"
  for _ in {1..3}; do
    if launchctl bootstrap "gui/$UID_VALUE" "$plist"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

cat >"$ORCH_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$ORCH_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>-m</string>
    <string>$RUNTIME_VENV/bin/xianyu-auto</string>
    <string>serve</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8787</string>
  </array>
  <key>WorkingDirectory</key><string>$RUNTIME_HOME</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>XIANYU_AUTO_HOME</key><string>$RUNTIME_HOME</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$RUNTIME_HOME/logs/orchestrator.stdout.log</string>
  <key>StandardErrorPath</key><string>$RUNTIME_HOME/logs/orchestrator.stderr.log</string>
</dict>
</plist>
PLIST

cat >"$BACKUP_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$BACKUP_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$RUNTIME_VENV/bin/python</string>
    <string>$RUNTIME_HOME/bin/backup_local.py</string>
  </array>
  <key>WorkingDirectory</key><string>$RUNTIME_HOME</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>XIANYU_AUTO_HOME</key><string>$RUNTIME_HOME</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>$RUNTIME_HOME/logs/backup.stdout.log</string>
  <key>StandardErrorPath</key><string>$RUNTIME_HOME/logs/backup.stderr.log</string>
</dict>
</plist>
PLIST

plutil -lint "$ORCH_PLIST" "$BACKUP_PLIST"
chmod 600 "$ORCH_PLIST" "$BACKUP_PLIST"
bootstrap_agent "$ORCH_PLIST"
bootstrap_agent "$BACKUP_PLIST"
launchctl kickstart -k "gui/$UID_VALUE/$ORCH_LABEL"

for _ in {1..30}; do
  if curl --fail --silent --show-error http://127.0.0.1:8787/api/health >/dev/null; then
    echo "本地编排服务已启动：http://127.0.0.1:8787"
    echo "运行目录：$RUNTIME_HOME"
    echo "当前默认模式为 dry-run；真实发布前必须完成凭据录入和验证。"
    exit 0
  fi
  sleep 1
done

echo "服务在 30 秒内未通过健康检查。请查看：" >&2
echo "$RUNTIME_HOME/logs/orchestrator.stderr.log" >&2
exit 1
