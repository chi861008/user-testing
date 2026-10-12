/* User Testing website SDK. Load on every page of the tested website after consent. */
(function () {
  "use strict";
  var script = document.currentScript;
  if (!script) return;
  var platform = new URL(script.src).origin;
  var storageKey = "ut-run:" + platform;
  var params = new URLSearchParams(location.search);
  var run = params.get("ut_run");
  if (!run && params.get("liff.state")) {
    try { run = new URLSearchParams(decodeURIComponent(params.get("liff.state")).split("?")[1] || "").get("ut_run"); } catch (_) {}
  }
  if (run && /^r[A-Za-z0-9_-]{20,80}$/.test(run)) {
    try { sessionStorage.setItem(storageKey, JSON.stringify({ run: run, at: Date.now() })); } catch (_) {}
  } else {
    try {
      var saved = JSON.parse(sessionStorage.getItem(storageKey) || "null");
      run = saved && Date.now() - saved.at < 4 * 3600000 ? saved.run : null;
    } catch (_) { run = null; }
  }
  if (!run || !/^r[A-Za-z0-9_-]{20,80}$/.test(run)) return;

  var endpoint = platform + "/api/instrument/" + encodeURIComponent(run);
  var active = "", kind = "", cond = null, completed = false, queue = [], busy = false, pendingName = "", pendingAt = 0;
  var eventPrefix = Math.random().toString(36).slice(2), eventNumber = 0;
  var tickTimer, syncTimer, stopped = false;
  var lastActive = Date.now();
  function screen() {
    var node = document.querySelector("[data-ut-screen]");
    return ((node && node.getAttribute("data-ut-screen")) || location.pathname || "/").slice(0, 80);
  }
  function elementKey(node) {
    var parts = [];
    while (node && node.nodeType === 1 && node !== document.body && parts.length < 5) {
      if (node.hasAttribute("data-ut-id")) return node.getAttribute("data-ut-id").slice(0, 100);
      var tag = node.tagName.toLowerCase(), n = 1, prev = node;
      while ((prev = prev.previousElementSibling)) if (prev.tagName === node.tagName) n++;
      parts.unshift(tag + ":nth-of-type(" + n + ")");
      node = node.parentElement;
    }
    return parts.join(" > ").slice(0, 100);
  }
  function enqueue(type, data) {
    if (stopped) return;
    queue.push({ id: eventPrefix + "_" + (++eventNumber), type: type, data: data || {} });
    if (queue.length >= 10 || type === "ready" || type === "complete") flush();
  }
  async function flush() {
    if (stopped) return;
    if (busy || !queue.length) return;
    busy = true;
    var batch = queue.splice(0, 30);
    try {
      var response = await fetch(endpoint, { method: "POST", mode: "cors", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ events: batch }), credentials: "omit", keepalive: true });
      if (!response.ok && response.status !== 403 && response.status !== 404) queue.unshift.apply(queue, batch);
    } catch (_) { queue.unshift.apply(queue, batch); }
    if (queue.length > 100) queue.splice(0, queue.length - 100);
    busy = false;
  }
  function checkComplete(name, clicked) {
    if (!active || kind !== "test" || completed || !cond) return;
    if (cond.type === "event" && cond.text === name ||
        cond.type === "click" && cond.text === clicked ||
        cond.type === "path" && location.pathname === cond.text ||
        cond.type === "marker" && document.querySelector('[data-ut-complete="' + CSS.escape(cond.text) + '"]')) {
      completed = true;
      enqueue("complete");
    }
  }
  window.UserTesting = { complete: function (name) { pendingName = String(name || ""); pendingAt = Date.now(); checkComplete(pendingName); },
    screen: screen, run: run };
  async function sync() {
    if (stopped) return;
    try {
      var response = await fetch(endpoint, { mode: "cors", credentials: "omit", cache: "no-store" });
      if (!response.ok) return;
      var state = await response.json();
      if (state.ended) {
        stopped = true; queue = [];
        clearInterval(tickTimer); clearInterval(syncTimer);
        try { sessionStorage.removeItem(storageKey); } catch (_) {}
        return;
      }
      if (state.activeTask !== active) { active = state.activeTask || ""; completed = false; lastActive = Date.now(); if (Date.now() - pendingAt > 5000) pendingName = ""; }
      kind = state.kind || ""; cond = state.cond || null;
      checkComplete(pendingName);
    } catch (_) {}
  }
  document.addEventListener("click", function (event) {
    if (!active || completed || !event.isTrusted) return;
    var node = event.target && event.target.closest ? event.target.closest("[data-ut-id],a,button,input,select,textarea,label,[role=button],[onclick]") : null;
    var target = node || event.target;
    if (!target || target.nodeType !== 1) return;
    var key = elementKey(target);
    enqueue("click", { screen: screen(), element: key, x: Math.max(0, Math.min(1, event.clientX / innerWidth)),
      y: Math.max(0, Math.min(1, (scrollY + event.clientY) / Math.max(document.documentElement.scrollHeight, innerHeight))),
      w: innerWidth, h: innerHeight, interactive: !!node });
    if (cond && cond.type === "click" && cond.text === key) checkComplete("", key);
  }, true);
  tickTimer = setInterval(function () {
    var at = Date.now();
    if (active && document.visibilityState === "visible") enqueue("active", { ms: Math.min(5000, Math.max(0, at - lastActive)) });
    lastActive = at;
    checkComplete("");
    flush();
  }, 3000);
  document.addEventListener("visibilitychange", function () { lastActive = Date.now(); if (document.visibilityState === "visible") sync(); else flush(); });
  window.addEventListener("pagehide", flush);
  enqueue("ready");
  sync();
  syncTimer = setInterval(sync, 2500);
})();
