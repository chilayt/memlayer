import { execFile as execFileCallback } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { mkdir, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";
import { memoryEventId, normalizeProjectRoot, projectKey, requestJson } from "./bridge.js";
import type { MemoryEvent } from "./bridge.js";

const execFile = promisify(execFileCallback);

export interface ShadowCapture {
  event: MemoryEvent;
  query?: string;
  baselineFactIds?: string[];
}

export interface ProjectResolver {
  resolve(cwd: string): Promise<string | null>;
}

export interface WorkerOptions {
  baseUrl: string;
  spoolFile: string;
  projectResolver?: ProjectResolver;
  requestTimeoutMs?: number;
  queueLimit?: number;
  maxTextChars?: number;
  captureAssistant?: boolean;
  /** Grayscale: drop captures whose project key fails this predicate. */
  captureFilter?: (projectKey: string) => boolean;
  closeTimeoutMs?: number;
  onLog?: (level: "info" | "warn" | "error", message: string) => void;
}

export class DirectCwdResolver implements ProjectResolver {
  async resolve(cwd: string): Promise<string | null> {
    return normalizeProjectRoot(cwd) || null;
  }
}

export function createProjectResolver(mode: string | undefined): ProjectResolver {
  if (mode === "cwd") return new DirectCwdResolver();
  if (mode === "env") {
    return { async resolve() { return process.env.MEMLAYER_PROJECT_ROOT || process.cwd() || null; } };
  }
  return new GitRootResolver();
}

export class GitRootResolver implements ProjectResolver {
  private readonly cache = new Map<string, Promise<string | null>>();

  resolve(cwd: string): Promise<string | null> {
    const key = projectKey(cwd);
    let pending = this.cache.get(key);
    if (!pending) {
      pending = execFile("git", ["-C", cwd, "rev-parse", "--show-toplevel"], {
        timeout: 1000,
        windowsHide: true,
        maxBuffer: 64 * 1024,
      }).then(({ stdout }) => {
        const root = stdout.trim();
        return root ? normalizeProjectRoot(root) : null;
      }).catch(() => null);
      this.cache.set(key, pending);
      if (this.cache.size > 256) this.cache.delete(this.cache.keys().next().value!);
    }
    return pending;
  }
}

export function redactSecrets(text: string): string {
  const key = "(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|secret|password)";
  return text
    .replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, "[REDACTED PRIVATE KEY]")
    .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]{12,}/g, "Bearer [REDACTED]")
    .replace(/\b(?:sk|tvly)-[A-Za-z0-9_-]{12,}\b/g, "[REDACTED API KEY]")
    // JSON and JS object forms, including quoted keys and escaped strings.
    .replace(new RegExp('("' + key + '")(\\s*:\\s*)"(?:[^"\\\\]|\\\\.)*"', "gi"),
      (_match, name: string, separator: string) => name + separator + '"[REDACTED]"')
    .replace(new RegExp("('" + key + "')(\\s*:\\s*)'(?:[^'\\\\]|\\\\.)*'", "gi"),
      (_match, name: string, separator: string) => name + separator + "'[REDACTED]'")
    .replace(new RegExp("\\b(" + key + ")(\\s*[:=]\\s*)(?:\"[^\"]*\"|'[^']*'|[^\\s,;]+)", "gi"),
      (_match, name: string, separator: string) => name + separator + "[REDACTED]");
}

export function messageText(message: any): string {
  if (typeof message?.content === "string") return message.content;
  if (!Array.isArray(message?.content)) return "";
  return message.content.filter((part: any) => part?.type === "text" && typeof part.text === "string")
    .map((part: any) => part.text).join("");
}

export async function captureSessionMessage(session: any, event: any, options: {
  resolver: ProjectResolver;
  maxTextChars?: number;
  captureAssistant?: boolean;
}): Promise<ShadowCapture | null> {
  const role = event?.type === "user/message" ? "user"
    : event?.type === "assistant/message" ? "assistant" : null;
  if (!role || (role === "assistant" && options.captureAssistant === false)) return null;
  const message = role === "user" ? event.data : event.data?.message;
  const source = message?.source ?? event.data?.source;
  if (!message || (typeof source?.kind === "string" && source.kind.startsWith("plugin")) || source?.plugin === "dsh-memlayer" || source?.form === "notice") return null;
  if (role === "user" && source?.kind !== "user") return null;
  const text = messageText(message).trim();
  if (!text || typeof session?.id !== "string" || !Number.isInteger(event.seq)) return null;
  const cwd = session.header?.cwd;
  if (typeof cwd !== "string" || !cwd) return null;
  const projectId = await options.resolver.resolve(cwd);
  if (!projectId) return null;
  const sourceId = session.id + ":" + event.seq;
  const timestamp = Number.isFinite(event.time) ? new Date(event.time).toISOString() : new Date().toISOString();
  const clipped = redactSecrets(text).slice(0, options.maxTextChars ?? 12000);
  const memoryEvent: MemoryEvent = {
    event_id: memoryEventId({ projectId, sessionId: session.id, sourceId, role }),
    project: projectKey(projectId),
    projectId,
    sessionId: session.id,
    role,
    subject: role === "user" ? "user" : "assistant",
    text: clipped,
    perspective: role === "user" ? "self" : "relayed",
    confirmed: false,
    at: timestamp,
    source_msg: sourceId,
    tags: ["dsh", "raw-capture"],
    host: "dsh",
  };
  return { event: memoryEvent, ...(role === "user" ? { query: clipped } : {}) };
}

export class MemlayerWorker {
  readonly resolver: ProjectResolver;
  private readonly options: Required<Omit<WorkerOptions, "onLog" | "projectResolver" | "captureFilter">> & { onLog?: WorkerOptions["onLog"]; captureFilter?: WorkerOptions["captureFilter"] };
  private pending: ShadowCapture[] = [];
  private readonly inflight = new Set<Promise<boolean>>();
  private closePromise?: Promise<void>;
  private accepting = true;
  private closeTimedOut = false;
  private persistScheduled = false;
  private persistDirty = false;
  private readonly abort = new AbortController();
  private stopped = false;
  private pumping?: Promise<void>;
  private persistChain: Promise<void> = Promise.resolve();
  private failures = 0;
  private offered = 0;
  private delivered = 0;
  private dropped = 0;
  private errors = 0;

  constructor(options: WorkerOptions) {
    this.options = {
      requestTimeoutMs: 300,
      queueLimit: 256,
      maxTextChars: 12000,
      captureAssistant: true,
      closeTimeoutMs: 2000,
      ...options,
    };
    this.resolver = options.projectResolver ?? new GitRootResolver();
    this.loadSpool();
  }

  private log(level: "info" | "warn" | "error", message: string): void {
    this.options.onLog?.(level, message);
  }

  private loadSpool(): void {
    if (!existsSync(this.options.spoolFile)) return;
    try {
      const rows = readFileSync(this.options.spoolFile, "utf8").split(/\r?\n/);
      for (const row of rows) {
        if (!row.trim()) continue;
        try {
          const value = JSON.parse(row);
          if (value?.event?.event_id && value?.event?.project) {
            this.pending.push(value);
          }
        } catch { /* tolerate a truncated line from an older crash */ }
      }
      if (this.pending.length > this.options.queueLimit) this.pending.length = this.options.queueLimit;
      if (this.pending.length) this.schedulePump();
    } catch (error) {
      this.log("error", "memlayer: cannot read spool: " + String(error));
    }
  }

  private schedulePersist(): void {
    this.persistDirty = true;
    if (this.persistScheduled) return;
    this.persistScheduled = true;
    const destination = this.options.spoolFile;
    this.persistChain = this.persistChain.then(async () => {
      try {
        do {
          this.persistDirty = false;
          const snapshot = this.pending.map(value => JSON.stringify(value)).join("\n")
            + (this.pending.length ? "\n" : "");
          await mkdir(path.dirname(destination), { recursive: true });
          const temp = destination + ".tmp-" + process.pid + "-" + Math.random().toString(16).slice(2);
          await writeFile(temp, snapshot, "utf8");
          await rename(temp, destination);
        } while (this.persistDirty);
      } finally {
        this.persistScheduled = false;
      }
    }).catch(error => {
      this.errors++;
      this.log("error", "memlayer: spool write failed: " + String(error));
    });
  }

  private schedulePump(): void {
    if (this.stopped || this.pumping || !this.pending.length) return;
    this.pumping = this.pump().finally(() => {
      this.pumping = undefined;
      if (!this.stopped && this.pending.length) this.schedulePump();
    });
  }

  private async sleep(ms: number): Promise<void> {
    if (this.stopped) return;
    await new Promise<void>(resolve => {
      const timer = setTimeout(resolve, ms);
      this.abort.signal.addEventListener("abort", () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    });
  }

  private async pump(): Promise<void> {
    // First attempt always runs even during close; only retries respect stopped.
    let firstAttempt = true;
    while ((firstAttempt || !this.stopped) && this.pending.length) {
      firstAttempt = false;
      const capture = this.pending[0]!;
      try {
        await requestJson(this.options.baseUrl, "/v1/shadow", {
          method: "POST",
          body: capture,
          timeoutMs: this.options.requestTimeoutMs,
          signal: this.abort.signal,
        });
        this.pending.shift();
        this.delivered++;
        this.failures = 0;
        this.schedulePersist();
      } catch (error) {
        if (this.stopped) break;
        this.errors++;
        this.failures++;
        const backoff = Math.min(5000, 250 * Math.pow(2, Math.min(5, this.failures - 1)));
        if (this.failures === 1 || this.failures % 10 === 0) {
          this.log("warn", "memlayer: bridge unavailable; capture remains spooled: " + String(error));
        }
        await this.sleep(backoff);
      }
    }
  }

  offerCapture(capture: ShadowCapture): boolean {
    if (this.stopped || this.pending.length >= this.options.queueLimit) {
      this.dropped++;
      if (this.dropped === 1 || this.dropped % 100 === 0) this.log("warn", "memlayer: queue full; capture dropped");
      return false;
    }
    this.pending.push(capture);
    this.offered++;
    this.schedulePersist();
    this.schedulePump();
    return true;
  }

  offerSessionEvent(session: any, event: any): Promise<boolean> {
    if (!this.accepting) return Promise.resolve(false);
    const task = this.captureAndOffer(session, event);
    this.inflight.add(task);
    void task.finally(() => this.inflight.delete(task)).catch(() => undefined);
    return task;
  }

  private async captureAndOffer(session: any, event: any): Promise<boolean> {
    try {
      const capture = await captureSessionMessage(session, event, {
        resolver: this.resolver,
        maxTextChars: this.options.maxTextChars,
        captureAssistant: this.options.captureAssistant,
      });
      if (!capture) return false;
      // Grayscale capture boundary: never spool/ship other projects' content.
      if (this.options.captureFilter && !this.options.captureFilter(capture.event.project)) {
        return false;
      }
      return this.offerCapture(capture);
    } catch (error) {
      this.errors++;
      this.log("warn", "memlayer: capture skipped: " + String(error));
      return false;
    }
  }

  async projectForSession(session: any): Promise<string | null> {
    const cwd = session?.header?.cwd ?? session?.cwd;
    const resolved = typeof cwd === "string" && cwd ? await this.resolver.resolve(cwd) : null;
    if (process.env.MEMLAYER_DEBUG === "1") {
      this.log("warn", "memlayer debug: headerCwd=" + String(session?.header?.cwd)
        + " cwd=" + String(cwd) + " resolved=" + String(resolved)
        + " processCwd=" + process.cwd());
    }
    return resolved;
  }

  async recall(projectId: string, query: string, limit = 5, signal?: AbortSignal): Promise<any[]> {
    const params = new URLSearchParams({
      project: projectKey(projectId),
      q: query,
      limit: String(limit),
      confirmed_only: "true",
      include_inferred: "false",
      mode: "current",
    });
    const result = await requestJson(this.options.baseUrl, "/v1/facts?" + params, {
      timeoutMs: this.options.requestTimeoutMs,
      signal,
    });
    return Array.isArray(result.facts) ? result.facts : [];
  }

  stats(): Record<string, number | boolean> {
    return { offered: this.offered, delivered: this.delivered, dropped: this.dropped,
      errors: this.errors, pending: this.pending.length, stopped: this.stopped,
      inflight: this.inflight.size, closeTimedOut: this.closeTimedOut };
  }

  async flush(timeoutMs = 2000): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    while (this.pending.length && Date.now() < deadline && !this.stopped) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    await this.persistChain;
    return this.pending.length === 0;
  }

  close(): Promise<void> {
    if (!this.closePromise) this.closePromise = this.closeInternal();
    return this.closePromise;
  }

  private async closeInternal(): Promise<void> {
    this.accepting = false;
    const deadline = Date.now() + this.options.closeTimeoutMs;
    const bounded = async (pending: Promise<unknown>): Promise<boolean> => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        return await Promise.race([
          pending.then(() => true).catch(() => true),
          new Promise<boolean>(resolve => {
            timer = setTimeout(() => resolve(false), Math.max(0, deadline - Date.now()));
          }),
        ]);
      } finally {
        if (timer) clearTimeout(timer);
      }
    };
    await bounded(Promise.allSettled([...this.inflight]));
    // Wait for the current pump to finish its in-flight request BEFORE aborting.
    // Aborting first would cancel the request and leave the capture stuck in pending.
    if (this.pumping && !await bounded(this.pumping)) this.closeTimedOut = true;
    this.stopped = true;
    this.abort.abort();
    this.schedulePersist();
    if (!await bounded(this.persistChain)) this.closeTimedOut = true;
    if (this.closeTimedOut) this.log("warn", "memlayer: close deadline reached; pending captures remain spooled");
  }
}
