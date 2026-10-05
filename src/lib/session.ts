// Sessions: random IDs in an HttpOnly cookie, stored server-side only as a hash.

import { randomToken, sha256, TOKEN_PATTERN } from "./crypto.ts";
import { HttpError, type Env } from "./http.ts";

export const COOKIE = "__Host-pw_session";
// A session lasts 90 days from its last use (each use extends it), and ends after 30 days idle.
// Signing out or deleting the account revokes it at once.
export const SESSION_TTL = 90 * 24 * 3600;
export const IDLE_TTL = 30 * 24 * 3600;
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

export function sessionCookie(id: string): string {
  return `${COOKIE}=${id}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_TTL}`;
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
  ).bind(await sha256(id), userId, now, now + SESSION_TTL).run();
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
    await env.DB.prepare("UPDATE sessions SET last_seen_at = ?2, expires_at = ?3 WHERE id_hash = ?1")
      .bind(idHash, now, now + SESSION_TTL)
      .run();
    return { userId: row.user_id, idHash, renewedCookie: sessionCookie(id) };
  }
  return { userId: row.user_id, idHash };
}

export async function requireSession(env: Env, request: Request, now: number): Promise<Session> {
  const session = await getSession(env, request, now);
  if (!session) throw new HttpError(401, "Please sign in.");
  return session;
}
