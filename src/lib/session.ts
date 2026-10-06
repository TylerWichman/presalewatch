// Sessions: random IDs in an HttpOnly cookie, stored server-side only as a hash.

import { randomToken, sha256, TOKEN_PATTERN } from "./crypto.ts";
import { HttpError, type Env } from "./http.ts";

export const COOKIE = "__Host-pw_session";
// A session ends after 90 days without use, and in any case 1 year after sign-in (expires_at,
// never extended). Each use pushes the idle limit out again. Signing out or deleting the account
// revokes it at once.
export const IDLE_TTL = 90 * 24 * 3600;
export const ABSOLUTE_TTL = 365 * 24 * 3600;
const TOUCH_EVERY = 3600;

export interface Session {
  userId: string;
  idHash: string;
  /** Set when this request extended the session: send it back so the cookie is extended too. */
  renewedCookie?: string;
}

export function readCookie(request: Request): string | null {
  const header = request.headers.get("Cookie") || "";
  for (const part of header.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === COOKIE) {
      const value = rest.join("=");
      return TOKEN_PATTERN.test(value) ? value : null;
    }
  }
  return null;
}

/** The cookie lives as long as the session could: 90 days from now, but never past the 1-year cap. */
export function sessionCookie(id: string, maxAge = IDLE_TTL): string {
  return `${COOKIE}=${id}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${Math.max(0, Math.min(maxAge, IDLE_TTL))}`;
}

export function clearCookie(): string {
  return `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`;
}

/** Create a session. Any session the browser already had is deleted first (rotation on login). */
export async function createSession(env: Env, request: Request, userId: string, now: number): Promise<string> {
  const old = readCookie(request);
  if (old) await env.DB.prepare("DELETE FROM sessions WHERE id_hash = ?1").bind(await sha256(old)).run();
  const id = randomToken(32);
  await env.DB.prepare(
    "INSERT INTO sessions (id_hash, user_id, created_at, last_seen_at, expires_at) VALUES (?1, ?2, ?3, ?3, ?4)",
  ).bind(await sha256(id), userId, now, now + ABSOLUTE_TTL).run();
  return id;
}

/** The current session, or null if missing, expired, idle too long, or revoked. */
export async function getSession(env: Env, request: Request, now: number): Promise<Session | null> {
  const id = readCookie(request);
  if (!id) return null;
  const idHash = await sha256(id);
  const row = await env.DB.prepare(
    "SELECT user_id, last_seen_at, expires_at FROM sessions WHERE id_hash = ?1",
  ).bind(idHash).first<{ user_id: string; last_seen_at: number; expires_at: number }>();
  if (!row) return null;
  if (row.expires_at <= now || row.last_seen_at + IDLE_TTL <= now) {
    await env.DB.prepare("DELETE FROM sessions WHERE id_hash = ?1").bind(idHash).run();
    return null;
  }
  if (now - row.last_seen_at >= TOUCH_EVERY) {
    await env.DB.prepare("UPDATE sessions SET last_seen_at = ?2 WHERE id_hash = ?1").bind(idHash, now).run();
    return { userId: row.user_id, idHash, renewedCookie: sessionCookie(id, row.expires_at - now) };
  }
  return { userId: row.user_id, idHash };
}

export async function requireSession(env: Env, request: Request, now: number): Promise<Session> {
  const session = await getSession(env, request, now);
  if (!session) throw new HttpError(401, "Please sign in.");
  return session;
}
