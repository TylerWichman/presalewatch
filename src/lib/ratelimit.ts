// Fixed-window rate limits stored in D1. Keys are HMACs of the IP or email, never the raw value.

import { hmac } from "./crypto.ts";
import type { Env } from "./http.ts";

export interface Limit {
  scope: string;
  window: number; // seconds
  max: number;
}

export const LIMITS = {
  signinIp: [{ scope: "signin-ip", window: 900, max: 5 }, { scope: "signin-ip", window: 86400, max: 20 }],
  signinEmail: [{ scope: "signin-email", window: 900, max: 3 }, { scope: "signin-email", window: 86400, max: 10 }],
  // Caps all sign-in emails so an attack can't use up the Resend daily quota needed for alerts.
  signinGlobal: [{ scope: "signin-all", window: 86400, max: 60 }],
  verifyIp: [{ scope: "verify-ip", window: 900, max: 20 }],
  // Code tries, on top of the 5 per request: per IP, and per address across all its requests.
  codeIp: [{ scope: "code-ip", window: 900, max: 20 }, { scope: "code-ip", window: 86400, max: 60 }],
  codeEmail: [{ scope: "code-email", window: 900, max: 15 }, { scope: "code-email", window: 86400, max: 50 }],
  unsubscribeIp: [{ scope: "unsub-ip", window: 900, max: 20 }],
} satisfies Record<string, Limit[]>;

/** Count one hit against every limit; true if all are still within their max. */
export async function allow(env: Env, limits: Limit[], value: string, now: number): Promise<boolean> {
  let ok = true;
  for (const l of limits) {
    const key = await hmac(env.IP_HASH_SECRET, `${l.scope}:${l.window}:${value}`);
    const start = now - (now % l.window);
    const row = await env.DB.prepare(
      `INSERT INTO rate_limits (key, window_start, count) VALUES (?1, ?2, 1)
       ON CONFLICT (key, window_start) DO UPDATE SET count = count + 1
       RETURNING count`,
    ).bind(key, start).first<{ count: number }>();
    if (!row || row.count > l.max) ok = false;
  }
  return ok;
}
