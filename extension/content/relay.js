// relay.js — ISOLATED world content script.
//
// The transport bridge between the extension service worker (which has
// chrome.runtime) and the MAIN-world page.js (which has page access). Neither
// can reach the other directly, so this relay sits in the middle:
//
//   SW  --chrome.runtime-->  relay  --postMessage(+nonce)-->  page.js (MAIN)
//   SW  <--chrome.runtime--  relay  <--postMessage(+nonce)--  page.js (MAIN)
//
// A per-page random nonce is minted here and handed to page.js during a
// handshake; both sides check event.source===window and nonce, so a foreign
// script can't easily forge results back to the SW.

(() => {
  // Latest injection wins (see page.js note). No same-version skip: a fresh
  // relay must take over from an orphaned one left across an extension reload.
  const SELF = {};
  window.__webBridgeRelaySelf = SELF;

  const NONCE = Math.random().toString(36).slice(2) + Date.now().toString(36);
  let mainReady = false;
  const pendingToMain = []; // commands queued until MAIN handshake completes

  const post = (m) => window.postMessage(m, window.location.origin);

  function sayHello() {
    post({ __wb: "hello", nonce: NONCE });
  }

  function flush() {
    while (mainReady && pendingToMain.length) post(pendingToMain.shift());
  }

  // page.js -> relay
  window.addEventListener("message", (ev) => {
    if (window.__webBridgeRelaySelf !== SELF) return; // superseded
    if (ev.source !== window) return;
    const m = ev.data;
    if (!m || typeof m !== "object") return;

    if (m.__wb === "main-ready") {
      sayHello(); // (re)offer the nonce; MAIN may have loaded after us
      return;
    }
    if (m.nonce !== NONCE) return; // everything below must carry our nonce

    if (m.__wb === "main-hello") {
      mainReady = true;
      flush();
    } else if (m.__wb === "res") {
      chrome.runtime.sendMessage({ type: "WB_RESULT", id: m.id, ok: m.ok, data: m.data, error: m.error });
    } else if (m.__wb === "progress") {
      chrome.runtime.sendMessage({ type: "WB_PROGRESS", id: m.id, stage: m.stage });
    }
  });

  // SW -> relay -> page.js. The SW acks synchronously; the real result comes
  // back later as a WB_RESULT runtime message (which re-wakes a recycled SW).
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (window.__webBridgeRelaySelf !== SELF) return false; // superseded
    if (!msg || msg.type !== "WB_CMD") return false;
    try { sendResponse({ acked: true, ready: mainReady }); } catch (_) {}
    const cmd = { __wb: "cmd", nonce: NONCE, ...msg.cmd };
    if (mainReady) post(cmd);
    else { pendingToMain.push(cmd); sayHello(); }
    return false;
  });

  sayHello();
})();
