// Test harness: a D1-compatible wrapper over node:sqlite with the real migrations applied,
// a fake fetch for Turnstile and Resend, and a way to call routes through the CSRF middleware.

import { readdirSync, readFileSync } from "node:fs";
import { DatabaseSync, type StatementSync } from "node:sqlite";
import { csrfCheck } from "../../functions/api/_middleware.ts";
import { securityHeaders, type Ctx, type Env, type Handler } from "../../src/lib/http.ts";

type Value = string | number | null;

class Statement {
  db: FakeD1;
  sql: string;
  params: Value[] = [];
  constructor(db: FakeD1, sql: string) {
    this.db = db;
    this.sql = sql;
  }
  bind(...params: Value[]) {
    for (const p of params) if (p === undefined) throw new Error("undefined bind value");
    const s = new Statement(this.db, this.sql);
    s.params = params;
    return s;
  }
  stmt(): StatementSync {
    return this.db.sqlite.prepare(this.sql);
  }
  async first<T>(): Promise<T | null> {
    return ((this.stmt().get(...this.params) as T | undefined) ?? null);
  }
  async all<T>(): Promise<{ results: T[] }> {
    return { results: this.stmt().all(...this.params) as T[] };
  }
  async run() {
    const r = this.stmt().run(...this.params);
    return { success: true, meta: { changes: Number(r.changes) } };
  }
}

export class FakeD1 {
  sqlite: DatabaseSync;
  queries: string[] = [];
  constructor() {
    this.sqlite = new DatabaseSync(":memory:");
    this.sqlite.exec("PRAGMA foreign_keys = ON");
    const dir = new URL("../../migrations/", import.meta.url);
    for (const f of readdirSync(dir).filter((f) => f.endsWith(".sql")).sort()) {
      this.sqlite.exec(readFileSync(new URL(f, dir), "utf8"));
    }
  }
  prepare(sql: string) {
    this.queries.push(sql);
    return new Statement(this, sql);
  }
  async batch(stmts: Statement[]) {
    this.sqlite.exec("BEGIN");
    try {
      const out = stmts.map((s) => ({ results: s.stmt().all(...s.params), success: true, meta: {} }));
      this.sqlite.exec("COMMIT");
      return out;
    } catch (err) {
      this.sqlite.exec("ROLLBACK");
      throw err;
    }
  }
  rows<T = Record<string, unknown>>(sql: string, ...params: Value[]): T[] {
    return this.sqlite.prepare(sql).all(...params) as T[];
  }
}

export const ORIGIN = "https://pouchit.net";

export function makeEnv(db = new FakeD1()): Env & { DB: FakeD1 & D1Database } {
  return {
    DB: db as FakeD1 & D1Database,
    APP_ORIGIN: ORIGIN,
    EMAIL_FROM: "PresaleWatch <alerts@pouchit.net>",
    RESEND_API_KEY: "re_test_key_not_real",
    TURNSTILE_SECRET: "turnstile_test_secret",
    UNSUBSCRIBE_SECRET: "u".repeat(48),
    IP_HASH_SECRET: "i".repeat(48),
  };
}

export interface SentMail {
  url: string;
  body: any;
}

/** Replace global fetch: Turnstile passes for token "good"; Resend calls are recorded. */
export function fakeFetch(opts: { resendStatus?: number; feed?: unknown } = {}) {
  const mail: SentMail[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.startsWith("https://challenges.cloudflare.com/")) {
      const token = (init?.body as FormData).get("response");
      const good = token === "good";
      return Response.json({ success: good, hostname: "pouchit.net", action: "signin" });
    }
    if (url.startsWith("https://api.resend.com/")) {
      mail.push({ url, body: JSON.parse(String(init?.body)) });
      return new Response("{}", { status: opts.resendStatus ?? 200 });
    }
    if (url === `${ORIGIN}/alerts.json`) return Response.json(opts.feed);
    throw new Error(`unexpected fetch ${url}`);
  }) as typeof fetch;
  return { mail, restore: () => (globalThis.fetch = real) };
}

export interface CallOpts {
  method?: string;
  body?: unknown;
  cookie?: string;
  origin?: string | null;
  contentType?: string | null;
  ip?: string;
  params?: Record<string, string>;
  query?: string;
}

/** Call a route the way Pages would: middleware CSRF check, then the handler, then waitUntil work. */
export async function call(handler: Handler, env: Env, path: string, opts: CallOpts = {}): Promise<Response> {
  const method = opts.method ?? "POST";
  const headers = new Headers();
  if (opts.origin !== null) headers.set("Origin", opts.origin ?? ORIGIN);
  if (opts.contentType !== null && method !== "GET" && method !== "DELETE") headers.set("Content-Type", opts.contentType ?? "application/json");
  if (opts.cookie) headers.set("Cookie", opts.cookie);
  headers.set("CF-Connecting-IP", opts.ip ?? "203.0.113.7");
  const request = new Request(`${ORIGIN}${path}${opts.query ?? ""}`, {
    method,
    headers,
    body: opts.body === undefined || method === "GET" ? undefined : typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body),
  });
  const blocked = csrfCheck(request, env);
  if (blocked) return blocked;
  const pending: Promise<unknown>[] = [];
  const ctx: Ctx = { request, env, params: opts.params ?? {}, waitUntil: (p) => pending.push(p) };
  const res = await handler(ctx);
  await Promise.all(pending);
  securityHeaders(res.headers);
  return res;
}

export function cookieFrom(res: Response): string {
  const set = res.headers.get("Set-Cookie") || "";
  return set.split(";")[0];
}

/** The magic-link token from the most recent sign-in email. */
export function tokenFromMail(mail: SentMail[]): string {
  const last = mail.filter((m) => m.url.endsWith("/emails")).at(-1);
  const match = /#token=([A-Za-z0-9_-]{43})/.exec(last?.body.text ?? "");
  if (!match) throw new Error("no sign-in link in mail");
  return match[1];
}

import { onRequestPost as requestLink } from "../../functions/api/auth/request.ts";
import { onRequestPost as verifyLink } from "../../functions/api/auth/verify.ts";

let ipCounter = 0;
export function freshIp(): string {
  ipCounter += 1;
  return `198.51.100.${ipCounter % 250}`;
}

/** Full magic-link sign-in; returns the session cookie ("name=value"). */
export async function signIn(env: Env, mail: SentMail[], address: string): Promise<string> {
  const ip = freshIp();
  await call(requestLink, env, "/api/auth/request", { body: { email: address, turnstileToken: "good" }, ip });
  const token = tokenFromMail(mail);
  const res = await call(verifyLink, env, "/api/auth/verify", { body: { token, confirm: true }, ip });
  if (res.status !== 200) throw new Error(`sign-in failed: ${res.status}`);
  return cookieFrom(res);
}
