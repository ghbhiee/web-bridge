#!/bin/bash
# Run the regression suite against a throwaway bridge, never the live one.
#
# The mock extension takes the hub's single extension slot — and since the real
# extension reconnects when it is displaced, the two end up kicking each other
# out, which both disturbs the user's browser and makes the tests flaky. A
# separate port + state dir keeps the suite entirely to itself.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${WEB_BRIDGE_TEST_PORT:-8795}"
STATE="$(mktemp -d /tmp/web-bridge-test.XXXXXX)"

cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$STATE"
}
trap cleanup EXIT

export WEB_BRIDGE_PORT="$PORT" WEB_BRIDGE_STATE="$STATE"
python3 "$HERE/server.py" >"$STATE/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 40); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break
  sleep 0.25
done

python3 "$HERE/test_mock_ext.py"
RC=$?
[ $RC -ne 0 ] && { echo "--- 测试服务器日志 ---"; tail -30 "$STATE/server.log"; }
exit $RC
