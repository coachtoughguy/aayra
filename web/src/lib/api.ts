import { config } from "./config";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

type TokenGetter = () => Promise<string | null>;
let getToken: TokenGetter = async () => null;
let onUnauthorized: (code: string) => void = () => {};

export function setTokenGetter(fn: TokenGetter) {
  getToken = fn;
}
export function setUnauthorizedHandler(fn: (code: string) => void) {
  onUnauthorized = fn;
}

function newKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export interface CallOptions {
  idempotencyKey?: string;
  auth?: boolean;
  skipKick?: boolean;
}

export async function api<T = any>(method: string, path: string, body?: unknown, opts: CallOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.auth !== false) {
    const token = await getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) headers["Idempotency-Key"] = opts.idempotencyKey || newKey();
  let res: Response;
  try {
    res = await fetch(`${config.apiUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "NETWORK", "Can't reach Aayra right now. Check your connection and try again.");
  }
  let json: any = null;
  try {
    json = await res.json();
  } catch {
    /* empty body */
  }
  if (!res.ok) {
    const e = json?.error || {};
    const err = new ApiError(res.status, e.code || "ERROR", e.message || `Request failed (${res.status})`, e.details || {});
    if (res.status === 401) onUnauthorized(err.code);
    throw err;
  }
  if (method !== "GET" && opts.auth !== false && !opts.skipKick) kickWorker();
  return (json?.data ?? json) as T;
}

export const get = <T = any>(path: string) => api<T>("GET", path);
export const post = <T = any>(path: string, body?: unknown, opts?: CallOptions) => api<T>("POST", path, body ?? {}, opts);
export const patch = <T = any>(path: string, body?: unknown) => api<T>("PATCH", path, body ?? {});
export const put = <T = any>(path: string, body?: unknown) => api<T>("PUT", path, body ?? {});
export const del = <T = any>(path: string) => api<T>("DELETE", path);

// Background jobs (file processing, lesson preparation, delivering work, grading) run when something
// kicks them, because a serverless host has no always-on worker. Every successful change kicks; kicks
// that arrive while one is running are merged into one follow-up run, so no queued job is missed.
let kickRunning = false;
let kickAgain = false;
export function kickWorker(): void {
  if (kickRunning) {
    kickAgain = true;
    return;
  }
  kickRunning = true;
  api<{ ran: number; waiting: number }>("POST", "/v1/worker/kick", {}, { skipKick: true })
    .then((r) => {
      if (r && r.waiting > 0) kickAgain = true;
      // Screens listen for this and refresh, so work delivered in the background shows up.
      if (r && r.ran > 0) window.dispatchEvent(new Event("aayra:jobs-ran"));
    })
    .catch(() => {})
    .finally(() => {
      kickRunning = false;
      if (kickAgain) {
        kickAgain = false;
        window.setTimeout(kickWorker, 300);
      }
    });
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return "Something went wrong.";
}

// Files: stub storage (local dev) is served by the API's /dev/storage routes.
export function fileUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  if (url.startsWith("stub://")) {
    const key = url.replace(/^stub:\/\/[^/]+\//, "").split("?")[0];
    return `${config.apiUrl}/dev/storage/${key}`;
  }
  return url;
}

export async function uploadToSignedUrl(uploadUrl: string, file: Blob, contentType?: string): Promise<void> {
  const type = contentType || (file as File).type || "application/octet-stream";
  if (uploadUrl.startsWith("stub://")) {
    const target = fileUrl(uploadUrl)!;
    const r = await fetch(target, { method: "PUT", body: file, headers: { "Content-Type": type } });
    if (!r.ok) throw new ApiError(r.status, "UPLOAD_FAILED", "The upload didn't go through. Please try again.");
    return;
  }
  const r = await fetch(uploadUrl, { method: "PUT", body: file, headers: { "Content-Type": type, "x-upsert": "false" } });
  if (!r.ok) throw new ApiError(r.status, "UPLOAD_FAILED", "The upload didn't go through. Please try again.");
}
