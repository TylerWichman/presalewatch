// Sign-in requests: one emailed link and one 6-digit code per request, sharing a login_tokens row,
// so using either one ends both. The code only works in the browser that asked for it: that
// browser holds a pending-login cookie whose hash is stored with the request.

import { hmac, randomToken, sha256, TOKEN_PATTERN } from "./crypto.ts";
import { renderWelcome, sendEmail } from "./email.ts";
import { log, type Env } from "./http.ts";
import { createSession } from "./session.ts";
import { artist, artistKey, FOLLOWS_MAX } from "./validate.ts";

export const TOKEN_TTL = 15 * 60;
export const CODE_ATTEMPTS = 5;
export const RESEND_WAIT = 30;
export const PENDING_COOKIE = "__Host-pw_pending";
export const CODE_PATTERN = /^[0-9]{6}$/;

/** Six digits from a secure random source, uniform (rejection sampling avoids modulo bias). */
export function randomCode(): string {
  const limit = 4_294_000_000; // largest multiple of 1,000,000 below 2^32
  const buf = new Uint32Array(1);
  do crypto.getRandomValues(buf);
  while (buf[0] >= limit);
  return String(buf[0] % 1_000_000).padStart(6, "0");
}

/** Keyed hash of a code, bound to its request: a stolen database row can't be brute-forced
 * offline without the server secret, and a code can't be moved to another request. */
export function codeHash(env: Env, tokenHash: string, code: string): Promise<string> {
  return hmac(env.IP_HASH_SECRET, `login-code:${tokenHash}:${code}`);
}

export function pendingCookie(value: string, maxAge = TOKEN_TTL): string {
  return `${PENDING_COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`;
}

export function clearPendingCookie(): string {
  return pendingCookie("", 0);
}

export function readPending(request: Request): string | null {
  for (const part of (request.headers.get("Cookie") || "").split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === PENDING_COOKIE) {
      const value = rest.join("=");
      return TOKEN_PATTERN.test(value) ? value : null;
    }
  }
  return null;
}

export interface NewRequest {
  token: string;
  code: string;
}

/** The artist from an "Alert me" sign-in, validated like any follow; null when there isn't one. */
export function pendingFollow(value: unknown): string | null {
  if (value === undefined || value === null || value === "") return null;
  const name = artist(value);
  return artistKey(name) ? name : null;
}

/** Store a sign-in request bound to `binding` (the pending-login cookie value). */
export async function createRequest(env: Env, address: string, binding: string, now: number, follow: string | null = null): Promise<NewRequest> {
  const token = randomToken(32);
  const code = randomCode();
  const tokenHash = await sha256(token);
  await env.DB.prepare(
    "INSERT INTO login_tokens (token_hash, email, expires_at, code_hash, attempts, request_binding_hash, created_at, pending_follow)" +
      " VALUES (?1, ?2, ?3, ?4, 0, ?5, ?6, ?7)",
  ).bind(tokenHash, address, now + TOKEN_TTL, await codeHash(env, tokenHash, code), await sha256(binding), now, follow).run();
  return { token, code };
}

export interface SignInOptions {
  /** The artist saved with the sign-in request ("Alert me"), followed now. */
  follow?: string | null;
  waitUntil?: (p: Promise<unknown>) => void;
}

/** The address's account (created on first sign-in) and a fresh session. Any other outstanding
 * sign-in requests for the address stop working. A pending follow is applied, and a first
 * sign-in gets the welcome email. Returns the new session ID. */
export async function completeSignIn(env: Env, request: Request, address: string, now: number, opts: SignInOptions = {}): Promise<string> {
  const [, , user] = await env.DB.batch([
    env.DB.prepare("UPDATE login_tokens SET used_at = ?2 WHERE email = ?1 AND used_at IS NULL").bind(address, now),
    env.DB.prepare("INSERT INTO users (id, email, unsub_nonce, created_at) VALUES (?1, ?2, ?3, ?4) ON CONFLICT (email) DO NOTHING")
      .bind(randomToken(16), address, randomToken(16), now),
    env.DB.prepare("SELECT id FROM users WHERE email = ?1").bind(address),
  ]);
  const userId = (user.results[0] as { id: string }).id;
  await env.DB.prepare("INSERT INTO preferences (user_id, updated_at) VALUES (?1, ?2) ON CONFLICT (user_id) DO NOTHING")
    .bind(userId, now)
    .run();
  if (opts.follow) await applyFollow(env, userId, opts.follow, now);
  await welcome(env, userId, now, opts.waitUntil);
  return createSession(env, request, userId, now);
}

/** Follow an artist for a user (no-op if already followed or at the follow limit). */
export async function applyFollow(env: Env, userId: string, name: string, now: number): Promise<void> {
  const key = artistKey(name);
  const count = await env.DB.prepare("SELECT COUNT(*) AS n FROM follows WHERE user_id = ?1").bind(userId).first<{ n: number }>();
  if (!key || (count?.n ?? 0) >= FOLLOWS_MAX) return;
  await env.DB.prepare(
    "INSERT INTO follows (user_id, artist, artist_key, created_at) VALUES (?1, ?2, ?3, ?4) ON CONFLICT (user_id, artist_key) DO NOTHING",
  ).bind(userId, name, key, now).run();
}

/** The one-time welcome email, on a user's first sign-in. Not sent (and not marked sent) while
 * POSTAL_ADDRESS is blank, since its footer must carry it. */
async function welcome(env: Env, userId: string, now: number, waitUntil?: (p: Promise<unknown>) => void): Promise<void> {
  const postal = (env.POSTAL_ADDRESS ?? "").trim();
  if (!postal || !waitUntil) return;
  // Atomic: only one sign-in can claim the welcome.
  const user = await env.DB.prepare("UPDATE users SET welcomed_at = ?2 WHERE id = ?1 AND welcomed_at IS NULL RETURNING email, unsub_nonce")
    .bind(userId, now)
    .first<{ email: string; unsub_nonce: string }>();
  if (!user) return;
  const prefs = await env.DB.prepare("SELECT follow_alerts, profit_alerts, profit_threshold FROM preferences WHERE user_id = ?1")
    .bind(userId)
    .first<{ follow_alerts: number; profit_alerts: number; profit_threshold: number }>();
  const follows = await env.DB.prepare("SELECT artist FROM follows WHERE user_id = ?1 ORDER BY created_at, artist COLLATE NOCASE")
    .bind(userId)
    .all<{ artist: string }>();
  const message = await renderWelcome(
    { id: userId, email: user.email, unsub_nonce: user.unsub_nonce },
    {
      follows: follows.results.map((f) => f.artist),
      followAlerts: Boolean(prefs?.follow_alerts ?? 1),
      profitAlerts: Boolean(prefs?.profit_alerts ?? 1),
      threshold: prefs?.profit_threshold ?? 30,
    },
    { origin: env.APP_ORIGIN, from: env.EMAIL_FROM, unsubscribeSecret: env.UNSUBSCRIBE_SECRET, postalAddress: postal },
  );
  waitUntil(
    sendEmail(env, message).catch((err: unknown) => log("error", { where: "welcome", kind: err instanceof Error ? err.message : "send failed" })),
  );
}
