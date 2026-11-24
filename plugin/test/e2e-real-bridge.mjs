import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";
import { MemlayerWorker } from "../dist/worker.js";
import { projectKey, requestJson } from "../dist/bridge.js";

const python = process.env.MEMLAYER_PYTHON || "python";
const bridge = fileURLToPath(new URL("../../memlayer/bridge.py", import.meta.url));
const temp = await mkdtemp(path.join(os.tmpdir(), "memlayer-real-e2e-"));
const projectRoot = path.join(temp, "fictional-work");
const project = projectKey(projectRoot);
const reservation = net.createServer();
await new Promise(resolve => reservation.listen(0, "127.0.0.1", resolve));
const port = reservation.address().port;
await new Promise(resolve => reservation.close(resolve));
const baseUrl = "http://127.0.0.1:" + port;
const child = spawn(python, [bridge, "--root", path.join(temp, "data"), "--port", String(port)], {
  stdio: "ignore",
  windowsHide: true,
  env: { ...process.env, MEMLAYER_SEMANTIC: "0" },
});
let worker;
try {
  let healthy = false;
  for (let i = 0; i < 60; i++) {
    try {
      const response = await fetch(baseUrl + "/health");
      if (response.ok) {
        assert.equal((await response.json()).version, "0.3.0");
        healthy = true;
        break;
      }
    } catch { /* bridge is starting */ }
    await delay(100);
  }
  assert.equal(healthy, true, "Python bridge did not become healthy");
  worker = new MemlayerWorker({ baseUrl, spoolFile: path.join(temp, "spool.jsonl") });
  worker.offerCapture({ event: {
    event_id: "real-e2e-1", project, subject: "user",
    text: "DSH 到 Python 真实桥接验收", confirmed: false, perspective: "self", host: "dsh",
  }, query: "真实桥接" });
  assert.equal(await worker.flush(4000), true);
  const status = await (await fetch(baseUrl + "/v1/status")).json();
  assert.equal(status.status.facts_total, 0, "shadow capture must not create facts");
  const journal = (await readFile(path.join(temp, "data", "shadow.jsonl"), "utf8")).trim().split(/\r?\n/);
  assert.equal(journal.length, 1);
  const row = JSON.parse(journal[0]);
  assert.equal(row.eventId, "real-e2e-1");
  assert.equal(row.injected, false);
  assert.equal(row.factsWritten, false);
  assert.equal(row.host, "dsh");
  const candidate = async (id, text) => (await requestJson(baseUrl, "/v1/candidates", { body: {
    project, subject: "author-note", text, source_event_id: id,
  }})).result.candidate;
  const original = await candidate("author-default", "夜巡标题默认使用夜巡前缀灯号");
  assert.deepEqual(await worker.recall(projectRoot, "夜巡标题"), []);
  const current = (await requestJson(baseUrl, "/v1/candidates/approve", { body: {
    candidate_id: original.id, expected_hash: original.content_hash,
  }})).result.fact_id;
  const exception = await candidate("author-exception", "夜巡标题仅在卷末改用卷终前缀");
  const final = (await requestJson(baseUrl, "/v1/candidates/approve-exception", { body: {
    candidate_id: exception.id, expected_hash: exception.content_hash, supersedes_fact_id: current,
  }})).result.fact_id;
  const recall = await worker.recall(projectRoot, "夜巡标题");
  assert.equal(recall.length, 1);
  assert.equal(recall[0].id, final);
  assert.ok(recall[0].text.includes(original.text) && recall[0].text.includes(exception.text));
  await requestJson(baseUrl, "/v1/facts/retract", { body: { fact_id: final, reason: "fictional test complete" } });
  assert.equal((await worker.recall(projectRoot, "夜巡标题")).length, 0);
  console.log(JSON.stringify({ ok: true, bridgeVersion: "0.3.0", shadowEvents: 1,
    approvedException: true, retraction: true }, null, 2));
} finally {
  if (worker) await worker.close();
  child.kill();
  await Promise.race([
    new Promise(resolve => child.once("exit", resolve)),
    delay(3000),
  ]);
  await rm(temp, { recursive: true, force: true });
}
