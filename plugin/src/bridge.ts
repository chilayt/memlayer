import { createHash } from "node:crypto";
import { realpathSync } from "node:fs";

export interface MemoryEvent {
  event_id: string;
  project: string;
  subject: string;
  text: string;
  perspective?: "self" | "relayed" | "inferred";
  confirmed?: boolean;
  at?: string;
  source_msg?: string;
  tags?: string[];
  host?: string;
  projectId?: string;
  sessionId?: string;
  role?: "user" | "assistant";
}

export function normalizeProjectRoot(input: string): string {
  let value = input.trim();
  // Memorix and DSH may refer to the same Git worktree through different
  // junctions. Canonicalize before hashing so they share one memory partition.
  try { value = realpathSync.native(value); } catch { /* non-existent probe paths stay lexical */ }
  value = value.replace(/\\/g, "/").replace(/\/{2,}/g, "/");
  if (/^[a-z]:\//i.test(value)) value = value[0]!.toUpperCase() + value.slice(1);
  value = value.replace(/\/+$/g, "");
  return value;
}

export function projectKey(projectId: string): string {
  const root = normalizeProjectRoot(projectId);
  const normalized = process.platform === "win32" || /^[a-z]:\//i.test(root) ? root.toLowerCase() : root;
  return "p_" + createHash("sha256").update(normalized).digest("hex").slice(0, 24);
}

export function memoryEventId(parts: {
  projectId: string;
  sessionId: string;
  sourceId: string;
  role: "user" | "assistant";
}): string {
  const canonical = JSON.stringify(["v1", normalizeProjectRoot(parts.projectId),
    parts.sessionId, parts.sourceId, parts.role]);
  return createHash("sha256").update(canonical).digest("hex");
}

export function localBridgeUrl(baseUrl: string): URL {
  const address = new URL(baseUrl);
  if (address.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(address.hostname) ||
      address.username || address.password || address.pathname !== "/" || address.search || address.hash) {
    throw new Error("Memlayer bridge must be a loopback HTTP origin");
  }
  return address;
}

export async function requestJson(baseUrl: string, path: string, options: {
  method?: "GET" | "POST";
  body?: unknown;
  timeoutMs?: number;
  signal?: AbortSignal;
} = {}): Promise<any> {
  const endpoint = localBridgeUrl(baseUrl);
  const destination = new URL(path, endpoint);
  if (destination.origin !== endpoint.origin) throw new Error("Memlayer request must stay on the local bridge");
  options.signal?.throwIfAborted();
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 300;
  const timer = setTimeout(() => controller.abort(new Error("memlayer request timeout")), timeoutMs);
  const abort = () => controller.abort(options.signal?.reason);
  options.signal?.addEventListener("abort", abort, { once: true });
  try {
    const response = await fetch(destination, {
      redirect: "error",
      method: options.method ?? (options.body === undefined ? "GET" : "POST"),
      headers: options.body === undefined ? undefined : { "content-type": "application/json" },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });
    const value = await response.json() as any;
    if (!response.ok || value?.ok === false) {
      throw new Error("memlayer HTTP " + response.status + ": " + JSON.stringify(value?.error ?? value));
    }
    return value;
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", abort);
  }
}
