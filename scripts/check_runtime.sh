#!/bin/bash
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_HOME="${XIANYU_AUTO_HOME:-$HOME/Library/Application Support/XianyuAuto}"
RUNTIME_VENV="$RUNTIME_HOME/venv"
UID_VALUE="$(id -u)"
failures=0

check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf 'OK    %s\n' "$label"
  else
    printf 'FAIL  %s\n' "$label"
    failures=$((failures + 1))
  fi
}

echo "设备"
sw_vers
uname -m
df -h "$RUNTIME_HOME" | tail -1
echo

echo "常驻服务"
if launchctl print "gui/$UID_VALUE/com.xianyu-auto.orchestrator" >/dev/null 2>&1; then
  echo "OK    orchestrator LaunchAgent 已加载"
else
  echo "FAIL  orchestrator LaunchAgent 未加载"
  failures=$((failures + 1))
fi
if curl --fail --silent http://127.0.0.1:8787/api/health; then
  echo
  echo "OK    编排服务健康检查"
else
  echo "FAIL  编排服务健康检查"
  failures=$((failures + 1))
fi
if curl --fail --silent http://127.0.0.1:8787/api/health \
  | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("feishu_listener") == "running" else 1)'; then
  echo "OK    飞书长连接运行中"
else
  echo "FAIL  飞书长连接未运行"
  failures=$((failures + 1))
fi
echo

echo "依赖与数据"
check "运行态 Python 虚拟环境" test -x "$RUNTIME_VENV/bin/xianyu-auto"
check "SQLite 数据库" test -f "$RUNTIME_HOME/data/xianyu_auto.db"
check "正式样品清单" test -f "$ROOT/outputs/samples/math-100-oral-cards/product.json"
echo

echo "外部服务"
if curl --fail --silent http://127.0.0.1:59188/health; then
  echo
  echo "OK    Ydisks 本地服务"
else
  echo "WARN  Ydisks 尚未启动或未安装"
fi
echo

echo "Keychain"
cd "$ROOT"
uv run python scripts/setup_credentials.py --check
check_status=$?
if [ "$check_status" -ne 0 ]; then
  echo "WARN  关键凭据尚未全部录入（dry-run 可继续运行）"
fi
echo

echo "电源与睡眠"
pmset -g | grep -E 'displaysleep|sleep|womp' || true
echo "提示：合盖仍会休眠；运行期间请接电源并保持开盖，或使用合盖不休眠方案后自行承担散热与耗电风险。"

exit "$failures"
