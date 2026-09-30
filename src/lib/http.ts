// Shared request/response helpers for the Pages Functions API.

import headers from "../../config/security_headers.json" with { type: "json" };
import { InputError } from "./validate.ts";

export interface Env {
  DB: D1Database;
  APP_ORIGIN: string;
  EMAIL_FROM: string;
  RESEND_API_KEY: string;
  TURNSTILE_SECRET: string;
  UNSUBSCRIBE_SECRET: string;
  IP_HASH_SECRET: string;
}

export interface Ctx {
  request: Request;
  env: Env;
  params: Record<string, string | string[]>;
  waitUntil(promise: Promise<unknown>): void;
}

export type Handler = (ctx: Ctx) => Promise<Response>;

export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function securityHeaders(h: Headers): Headers {
  for (const [name, value] of Object.entries(headers.common)) h.set(name, value);
  h.set("Content-Security-Policy", headers.api_csp);
  h.set("Cache-Control", "no-store");
  return h;
}

export function json(body: unknown, status = 200, extra?: HeadersInit): Response {
  const h = new Headers(extra);
  h.set("Content-Type", "application/json; charset=utf-8");
  return new Response(JSON.stringify(body), { status, headers: securityHeaders(h) });
}

/** User-facing errors carry a safe message; anything else becomes a generic 500. */
export function errorResponse(err: unknown, where: string): Response {
  if (err instanceof HttpError) return json({ error: err.message }, err.status);
  if (err instanceof InputError) return json({ error: err.message }, 400);
  log("error", { where, kind: err instanceof Error ? err.name : typeof err });
  return json({ error: "Something went wrong. Please try again." }, 500);
}

const BODY_LIMIT = 4096;

/** Parse a small JSON object body. The middleware has already checked Content-Type. */
export async function readJson(request: Request): Promise<Record<string, unknown>> {
  const length = Number(request.headers.get("Content-Length") || "0");
  if (length > BODY_LIMIT) throw new HttpError(413, "Request too large.");
  const text = await request.text();
  if (text.length > BODY_LIMIT) throw new HttpError(413, "Request too large.");
  let body: unknown;
  try {
    body = JSON.parse(text || "{}");
  } catch {
    throw new HttpError(400, "Invalid request.");
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new HttpError(400, "Invalid request.");
  return body as Record<string, unknown>;
}

export function clientIp(request: Request): string {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

export function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

// Structured logs with an allowlist of fields, so emails, tokens, and session IDs can't end up in them.
const LOG_FIELDS = new Set(["where", "kind", "status", "user", "count", "events", "sent", "skipped", "reason", "generated_at", "dry_run"]);

export function log(level: "info" | "warn" | "error", fields: Record<string, unknown>): void {
  const safe: Record<string, unknown> = { level };
  for (const [k, v] of Object.entries(fields)) if (LOG_FIELDS.has(k)) safe[k] = v;
  (level === "error" ? console.error : console.log)(JSON.stringify(safe));
}

/** Wrap a route so any thrown error becomes a safe response. */
export function route(handler: Handler): Handler {
  return async (ctx) => {
    try {
      return await handler(ctx);
    } catch (err) {
      return errorResponse(err, new URL(ctx.request.url).pathname);
    }
  };
}
