// Copy to config.js, or generate it with: bridge/gen_ext_config.sh
//
// config.js holds the shared token the service worker presents to the local
// bridge. It is gitignored on purpose — the token is what stops any page on the
// machine from driving your browser through the bridge.
export const BRIDGE_WS = "ws://127.0.0.1:8790/ws/ext";
export const BRIDGE_TOKEN = "put-your-token-here";
