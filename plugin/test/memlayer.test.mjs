import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";
import { memoryEventId, normalizeProjectRoot, projectKey } from "../dist/bridge.js";
import { MemlayerWorker, captureSessionMessage, redactSecrets } from "../dist/worker.js";
import { apply, auditInjection, killSwitchTripped, latestUserQuery, memlayerSource, projectAllowed, recallHeader, renderRecall, renderRecallContext } from "../dist/index.js";

async function tempdir() {
  return mkdtemp(path.join(os.tmpdir(), "dsh-memlayer-test-"));
}

async function server(handler) {
  const requests = [];
  const instance = http.createServer((req, res) => {
    let body = "";
    req.on("data", chunk => { body += chunk; });
    req.on("end", async () => {
      const parsed = body ? JSON.parse(body) : undefined;
      requests.push({ method: req.method, url: req.url, body: parsed });
      const result = await handler?.(req, parsed) ?? { ok: true, result: { received: true } };
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(result));
    });
  });
  await new Promise(resolve => instance.listen(0, "127.0.0.1", resolve));
  const address = instance.address();
  return { instance, requests, url: "http://127.0.0.1:" + address.port,
    close: () => new Promise(resolve => instance.close(resolve)) };
}

function capture(id = "event-1") {
  return { event: {
    event_id: id, project: "p_test", subject: "user", text: "影子捕获 " + id,
    confirmed: false, perspective: "self", host: "dsh",
  }, query: "影子" };
}

async function waitFor(predicate, timeout = 2500) {
  const deadline = Date.now() + timeout;
  while (!predicate() && Date.now() < deadline) await delay(20);
  return predicate();
}

test("项目路径规范化、存储键与事件 ID 稳定", () => {
  assert.equal(normalizeProjectRoot("i:\\example-project\\"), "I:/example-project");
  assert.equal(projectKey("I:/example-project"), projectKey("I:/example-project"));
  assert.match(projectKey("I:\\example-project"), /^p_[a-f0-9]{24}$/);
  assert.equal(projectKey("I:\\example-project"), projectKey("i:/example-project/"));
  const parts = { projectId: "I:\\example-project", sessionId: "s1", sourceId: "s1:4", role: "user" };
  assert.equal(memoryEventId(parts), memoryEventId({ ...parts }));
  assert.notEqual(memoryEventId(parts), memoryEventId({ ...parts, role: "assistant" }));
});

test("会话捕获排除插件注入、使用哈希项目键且原文不确认为事实", async () => {
  const resolver = { resolve: async () => "I:/example-project" };
  const session = { id: "s1", header: { cwd: "I:/example-project" } };
  const event = { type: "user/message", seq: 4, time: 1793900000000,
    data: { content: [{ type: "text", text: "请记住项目规则" }], source: { kind: "user" } } };
  const first = await captureSessionMessage(session, event, { resolver });
  assert.ok(first);
  assert.match(first.event.project, /^p_/);
  assert.equal(first.event.confirmed, false);
  assert.equal(first.event.event_id, memoryEventId({ projectId: "I:/example-project", sessionId: "s1", sourceId: "s1:4", role: "user" }));
  const injected = structuredClone(event);
  injected.data.source = { kind: "plugin:dsh-memlayer" };
  assert.equal(await captureSessionMessage(session, injected, { resolver }), null);
  const noGit = { resolve: async () => null };
  assert.equal(await captureSessionMessage(session, event, { resolver: noGit }), null);
});

test("捕获前屏蔽常见密钥与凭据", () => {
  const secret = redactSecrets("api_key=abc123456789 Bearer abcdefghijklmnop tvly-dev-123456789012345");
  assert.doesNotMatch(secret, /abc123456789|abcdefghijklmnop|tvly-dev-123456789012345/);
  assert.match(secret, /REDACTED/);
  const json = redactSecrets('{"api_key":"SYNTHETIC-123456","token":"SYNTHETIC-654321","apiKey":"SYNTHETIC-abcdef"}');
  assert.doesNotMatch(json, /SYNTHETIC-123456|SYNTHETIC-654321|SYNTHETIC-abcdef/);
  assert.match(json, /REDACTED/);
});

test("worker 投递影子事件并清空持久队列", async () => {
  const dir = await tempdir();
  const remote = await server();
  const worker = new MemlayerWorker({ baseUrl: remote.url, spoolFile: path.join(dir, "spool.jsonl") });
  assert.equal(worker.offerCapture(capture()), true);
  assert.equal(await worker.flush(), true);
  assert.equal(remote.requests.length, 1);
  assert.equal(remote.requests[0].url, "/v1/shadow");
  assert.equal(remote.requests[0].body.event.event_id, "event-1");
  await worker.close();
  assert.equal(await readFile(path.join(dir, "spool.jsonl"), "utf8"), "");
  await remote.close();
  await rm(dir, { recursive: true, force: true });
});

test("断服务时保留 spool，重启后自动补投", async () => {
  const dir = await tempdir();
  const spoolFile = path.join(dir, "spool.jsonl");
  const unavailable = await server();
  const baseUrl = unavailable.url;
  await unavailable.close();
  const worker = new MemlayerWorker({ baseUrl, spoolFile, requestTimeoutMs: 100 });
  worker.offerCapture(capture("offline-1"));
  await delay(50);
  await worker.close();
  const persisted = await readFile(spoolFile, "utf8");
  assert.match(persisted, /offline-1/);

  const remote = await server();
  const reopened = new MemlayerWorker({ baseUrl: remote.url, spoolFile, requestTimeoutMs: 300 });
  assert.equal(await reopened.flush(), true);
  assert.equal(remote.requests.length, 1);
  assert.equal(remote.requests[0].body.event.event_id, "offline-1");
  await reopened.close();
  await remote.close();
  await rm(dir, { recursive: true, force: true });
});

test("DSH shadow 插件注册真实钩子、不修改 pre-step 决策", async () => {
  const dir = await tempdir();
  const remote = await server();
  const handlers = new Map();
  let dispose;
  const ctx = {
    logger: { warn() {}, error() {}, debug() {} },
    get() { return undefined; },
    on(name, fn) { handlers.set(name, fn); return () => handlers.delete(name); },
    effect(fn) { dispose = fn; },
  };
  apply(ctx);
  assert.equal(handlers.size, 0, "capture requires explicit enablement and a project");
  apply(ctx, { enabled: true, mode: "shadow", baseUrl: remote.url, spoolFile: path.join(dir, "spool.jsonl"),
    projectResolver: "cwd", projectAllowlist: ["I:/example-project"] });
  assert.ok(handlers.has("session/event"));
  assert.ok(handlers.has("agent/pre-step"));

  const original = { kind: "enter", messages: [{ role: "user", content: [] }] };
  const decision = await handlers.get("agent/pre-step")({ messages: [], signal: new AbortController().signal }, async () => original);
  assert.equal(decision, original);

  const session = { id: "s-hook", header: { cwd: "I:/example-project" } };
  handlers.get("session/event")(session, { type: "user/message", seq: 1, time: Date.now(),
    data: { content: [{ type: "text", text: "hook capture" }], source: { kind: "user" } } });
  assert.equal(await waitFor(() => remote.requests.length === 1), true);
  assert.equal(remote.requests[0].body.event.text, "hook capture");
  assert.equal(remote.requests[0].body.injected, undefined);
  await dispose();
  await remote.close();
  await rm(dir, { recursive: true, force: true });
});

test("close 等待在途捕获且慢网络下保留 spool", async () => {
  const dir = await tempdir();
  const remote = await server();
  const worker = new MemlayerWorker({ baseUrl: remote.url, spoolFile: path.join(dir, "spool.jsonl"), closeTimeoutMs: 1000 });
  worker.resolver.resolve = () => new Promise(resolve => setTimeout(() => resolve("I:/example-project"), 80));
  const offered = worker.offerSessionEvent(
    { id: "close-inflight", header: { cwd: "I:/example-project" } },
    { type: "user/message", seq: 1, time: Date.now(), data: { content: [{ type: "text", text: "inflight" }], source: { kind: "user" } } },
  );
  await worker.close();
  assert.equal(await offered, true);
  assert.equal(remote.requests.length, 1);
  assert.equal(worker.stats().inflight, 0);
  await remote.close();

  const slow = await server(async () => { await delay(5000); return { ok: true }; });
  const slowWorker = new MemlayerWorker({ baseUrl: slow.url, spoolFile: path.join(dir, "slow-spool.jsonl"), requestTimeoutMs: 5000, closeTimeoutMs: 100 });
  slowWorker.offerCapture(capture("slow-close"));
  await delay(20);
  const started = Date.now();
  await slowWorker.close();
  assert.ok(Date.now() - started < 1000);
  assert.match(await readFile(path.join(dir, "slow-spool.jsonl"), "utf8"), /slow-close/);
  await slow.close();
  await rm(dir, { recursive: true, force: true });
});

test("active 查询提取兼容 headless 缺失 source 的输入", () => {
  assert.equal(latestUserQuery([{ content: [{ type: "text", text: "lone headless question" }] }]), "lone headless question");
  const ambiguous = [
    { content: [{ type: "text", text: "older" }] },
    { content: [{ type: "text", text: "could be assistant" }] },
  ];
  assert.equal(latestUserQuery(ambiguous), "");
  assert.equal(latestUserQuery([{ role: "assistant", content: [{ type: "text", text: "assistant" }] }]), "");
  assert.equal(latestUserQuery([{ role: "user", content: [{ type: "text", text: "user role" }] }]), "user role");
  assert.equal(latestUserQuery([{ source: { kind: "plugin" }, content: [{ type: "text", text: "injected" }] }]), "");
});

test("v4 注入使用 producer-owned source kind", () => {
  assert.deepEqual(memlayerSource(4), { kind: "plugin:dsh-memlayer" });
  assert.deepEqual(memlayerSource(3), { kind: "plugin", plugin: "dsh-memlayer" });
  assert.equal(latestUserQuery([{ source: memlayerSource(4), content: [{ type: "text", text: "memory" }] }]), "");
});

test("召回文本明确降级为历史证据且受预算限制", () => {
  const text = renderRecallContext([
    { text: "第一条记忆", recorded_at: "2026-11-07" },
    { text: "x".repeat(5000), recorded_at: "2026-11-07" },
  ], 500);
  assert.match(text, /reviewed historical evidence/);
  assert.match(text, /第一条记忆/);
  assert.match(text, /subject=unknown/);
  assert.match(text, /sources=unknown/);
  assert.ok(text.length < 500);
  assert.equal(renderRecallContext([], 500), "");
  // Conflicts resolve by newest valid_from, and the header says so.
  const ordered = renderRecallContext([
    { text: "旧判据", valid_from: "2026-09-01" },
    { text: "新判据", valid_from: "2026-11-07" },
  ], 2000);
  assert.match(ordered, /prefer the newest valid_from/);
  assert.ok(ordered.indexOf("新判据") < ordered.indexOf("旧判据"));
  const legacy = renderRecallContext([{ text: "旧判据", valid_from: "2026-09-01" }], 2000, false);
  assert.match(legacy, /Verify currency before use/);
  assert.doesNotMatch(legacy, /prefer the newest valid_from/);
  // Partial-answer authorization is a separate, toggleable header change.
  const withPartial = recallHeader({ newestFirst: true, partialAnswer: true });
  const withoutPartial = recallHeader({ newestFirst: true, partialAnswer: false });
  assert.match(withPartial, /covers only part of the question/);
  assert.doesNotMatch(withoutPartial, /covers only part of the question/);
  assert.match(withoutPartial, /prefer the newest valid_from/);
  const packOff = renderRecall([{ id: "a", text: "x" }], 2000, false, true);
  assert.match(packOff.text, /covers only part of the question/);
});

test("注入审计逐条记录事实与状态", async () => {
  const dir = await tempdir();
  try {
    const rendered = renderRecall([
      { id: "f1", text: "第一条", valid_from: "2026-11-01" },
      { id: "f2", text: "x".repeat(4000), valid_from: "2026-11-02" },
    ], 800);
    assert.deepEqual(rendered.includedIds, ["f1"]);
    assert.ok(rendered.text.includes("第一条"));
    auditInjection(dir, { sessionId: "s1", turn: 2, project: "p1", query: "问", reason: "ok",
      recalledIds: ["f1", "f2"], includedIds: rendered.includedIds,
      facts: [{ id: "f1", project: "p1", status: "current", subject: "user", valid_to: null }] });
    await delay(50);
    const raw = await readFile(path.join(dir, "injection-audit.jsonl"), "utf8");
    const record = JSON.parse(raw.trim());
    assert.equal(record.sessionId, "s1");
    assert.equal(record.reason, "ok");
    assert.deepEqual(record.recalledIds, ["f1", "f2"]);
    assert.deepEqual(record.includedIds, ["f1"]);
    assert.equal(record.facts[0].id, "f1");
    assert.equal(record.facts[0].status, "current");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("灰度守卫：项目白名单与急停开关", async () => {
  const someRoot = "I:/Projects/SomeProject";
  const someKey = projectKey(someRoot);
  assert.equal(projectAllowed(someRoot, undefined), false);
  assert.equal(projectAllowed(someRoot, []), false);
  assert.equal(projectAllowed(null, [someKey]), false);
  assert.equal(projectAllowed(someRoot, [someKey]), true);
  assert.equal(projectAllowed(someKey, [someKey]), true);
  assert.equal(projectAllowed(someRoot, ["i:/projects/someproject"]), true);
  assert.equal(projectAllowed("I:/Other/SomeProject", [someRoot, "SomeProject"]), false);
  assert.equal(projectAllowed("I:/Projects/AnotherProject", [someKey]), false);

  const dir = await tempdir();
  try {
    const kill = path.join(dir, "KILL");
    assert.equal(killSwitchTripped(undefined), false);
    assert.equal(killSwitchTripped(kill), false);
    const { writeFile } = await import("node:fs/promises");
    await writeFile(kill, "stop", "utf8");
    assert.equal(killSwitchTripped(kill), true);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
test("灰度捕获边界：白名单外项目不落 spool", async () => {
  const dir = await tempdir();
  const spool = path.join(dir, "spool.jsonl");
  try {
    const allowedKey = projectKey("I:/Projects/Allowed");
    const worker = new MemlayerWorker({
      baseUrl: "http://127.0.0.1:1", spoolFile: spool, requestTimeoutMs: 50,
      captureFilter: (key) => key === allowedKey,
    });
    const mkSession = (cwd) => ({ id: "s-" + cwd, header: { cwd, version: 4 } });
    const mkEvent = (text) => ({ kind: "message", message: { role: "user", content: [{ type: "text", text }] }, source: { kind: "user" } });
    // non-allowlisted project: capture must be dropped
    await worker.offerSessionEvent(mkSession("I:/Projects/Other"), mkEvent("other project secret"));
    await worker.flush(500);
    const spoolExists = await import("node:fs/promises").then(m => m.readFile(spool, "utf8").catch(() => ""));
    assert.equal(spoolExists.includes("other project secret"), false);
    await worker.close();
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
test("回归：相关度主序，旧但相关的事实不被新干扰挤出预算", () => {
  // recall order: answer first (highest score) but OLDEST valid_from; distractors newer.
  const facts = [
    { id: "answer", subject: "build", text: "构建走 CLI 全自动 + 编辑器半自动", valid_from: "2026-10-15" },
    { id: "d1", subject: "report", text: "日报由主会话接管 ".repeat(20), valid_from: "2026-10-16" },
    { id: "d2", subject: "media", text: "媒体服务输出带音轨 ".repeat(20), valid_from: "2026-10-25" },
    { id: "d3", subject: "pet", text: "吉祥物用 2.5D 立绘 ".repeat(20), valid_from: "2026-10-30" },
  ];
  const { includedIds } = renderRecall(facts, 400, true, false);
  assert.ok(includedIds.includes("answer"), "旧但相关的答案事实必须被渲染，不能被新干扰挤出");
  assert.equal(includedIds[0], "answer", "相关度最高的事实应排第一");
});

test("同主题冲突：新版本优先渲染", () => {
  const facts = [
    { id: "old", subject: "采集档", text: "采集跑 ark 档", valid_from: "2026-10-10" },
    { id: "new", subject: "采集档", text: "采集跑 deepseek-account 档", valid_from: "2026-10-16" },
  ];
  const { includedIds } = renderRecall(facts, 2400, true, false);
  assert.equal(includedIds[0], "new", "同主题应新版本在前");
});
test("DSH adapter restricts the bridge to loopback and refuses redirects", async () => {
  const { localBridgeUrl, requestJson } = await import("../dist/bridge.js");
  assert.throws(() => localBridgeUrl("https://example.invalid"), /loopback/u);
  assert.throws(() => localBridgeUrl("http://local.example.test"), /loopback/u);
  await assert.rejects(requestJson("http://127.0.0.1:8765", "//example.invalid"), /local bridge/u);
  const redirected = http.createServer((_request, response) => {
    response.writeHead(307, { location: "https://example.invalid" });
    response.end();
  });
  await new Promise(resolve => redirected.listen(0, "127.0.0.1", resolve));
  try {
    await assert.rejects(requestJson("http://127.0.0.1:" + redirected.address().port, "/health"));
  } finally {
    await new Promise(resolve => redirected.close(resolve));
  }
});
