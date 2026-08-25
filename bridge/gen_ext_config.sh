#!/bin/bash
# Regenerate extension/config.js from ~/.config/web-bridge/config.json
set -euo pipefail
cfg="$HOME/.config/web-bridge/config.json"
here="$(cd "$(dirname "$0")/.." && pwd)"
tok=$(python3 -c "import json;print(json.load(open('$cfg'))['token'])")
port=$(python3 -c "import json;print(json.load(open('$cfg'))['port'])")
cat > "$here/extension/config.js" <<EOF
export const BRIDGE_WS = "ws://127.0.0.1:${port}/ws/ext";
export const BRIDGE_TOKEN = "${tok}";
EOF
echo "wrote $here/extension/config.js"
