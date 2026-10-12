const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../static/ut-sdk.js"), "utf8");

test("admin HTML keeps its inline script intact", () => {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8");
  const start = html.indexOf("<script>");
  const end = html.indexOf("</script>", start + 8);
  assert.ok(start > 0 && end > start);
  assert.match(html.slice(end), /^<\/script>\s*<\/body>\s*<\/html>\s*$/);
  new Function(html.slice(start + 8, end));
});

test("LIFF state keeps the run and reports completion without form data", async () => {
  const token = "r" + "a".repeat(30);
  const requests = [];
  const timers = [];
  const session = new Map();
  const document = {
    currentScript: { src: "https://testing.example/static/ut-sdk.js" },
    visibilityState: "visible",
    addEventListener() {},
    querySelector() { return null; },
  };
  const window = { addEventListener() {} };
  const context = {
    URL, URLSearchParams, Date, Math, JSON, CSS: { escape: x => x },
    document, window, innerWidth: 390, innerHeight: 844,
    location: { search: "?liff.state=" + encodeURIComponent("/home?ut_run=" + token), pathname: "/home" },
    sessionStorage: { setItem: (k, v) => session.set(k, v), getItem: k => session.get(k) || null },
    setInterval: fn => { timers.push(fn); return timers.length; },
    fetch: async (url, options = {}) => {
      requests.push({ url, options });
      return { ok: true, status: 200, json: async () => ({ activeTask: "task-1", kind: "test", cond: { type: "event", text: "found" } }) };
    },
  };
  vm.runInNewContext(source, context);
  await new Promise(setImmediate);
  assert.ok(session.has("ut-run:https://testing.example"));
  assert.ok(requests.some(r => r.url.endsWith("/api/instrument/" + token)));
  window.UserTesting.complete("found");
  await new Promise(setImmediate);
  const events = requests.filter(r => r.options.body).flatMap(r => JSON.parse(r.options.body).events);
  assert.ok(events.some(e => e.type === "ready"));
  assert.ok(events.some(e => e.type === "complete"));
  assert.equal(events.some(e => "password" in (e.data || {})), false);
  assert.equal(timers.length, 2);
});
