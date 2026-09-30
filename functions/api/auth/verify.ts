// POST /api/auth/verify {token, confirm, next?}
// confirm=false: check the link and return the masked address it's for (nothing is used up).
// confirm=true: use up the token (single use), create the account if new, and start a session.

import { randomToken, sha256, TOKEN_PATTERN } from "../../../src/lib/crypto.ts";
import { clientIp, HttpError, json, nowSeconds, readJson, route } from "../../../src/lib/http.ts";
import { allow, LIMITS } from "../../../src/lib/ratelimit.ts";
import { createSession, sessionCookie } from "../../../src/lib/session.ts";
import { redirectPath } from "../../../src/lib/validate.ts";

const INVALID = "This sign-in link is invalid, expired, or already used. Request a new one.";

export function maskEmail(address: string): string {
  const [local, domain] = address.split("@");
  return `${local.slice(0, 1)}•••@${domain}`;
}

export const onRequestPost = route(async ({ request, env }) => {
  const body = await readJson(request);
  const now = nowSeconds();
  if (!(await allow(env, LIMITS.verifyIp, clientIp(request), now))) throw new HttpError(429, "Too many attempts. Try again later.");
  // Malformed and unknown tokens get the same answer.
  if (typeof body.token !== "string" || !TOKEN_PATTERN.test(body.token)) throw new HttpError(400, INVALID);
  const tokenHash = await sha256(body.token);

  if (body.confirm !== true) {
    const row = await env.DB.prepare(
      "SELECT email FROM login_tokens WHERE token_hash = ?1 AND used_at IS NULL AND expires_at > ?2",
    ).bind(tokenHash, now).first<{ email: string }>();
    if (!row) throw new HttpError(400, INVALID);
    return json({ ok: true, email: maskEmail(row.email) });
  }

  // Atomic single use: only one request can flip used_at from NULL.
  const used = await env.DB.prepare(
    "UPDATE login_tokens SET used_at = ?2 WHERE token_hash = ?1 AND used_at IS NULL AND expires_at > ?2 RETURNING email",
  ).bind(tokenHash, now).first<{ email: string }>();
  if (!used) throw new HttpError(400, INVALID);

  const [, , user] = await env.DB.batch([
    // Any other outstanding links for this address stop working.
    env.DB.prepare("UPDATE login_tokens SET used_at = ?2 WHERE email = ?1 AND used_at IS NULL").bind(used.email, now),
    env.DB.prepare("INSERT INTO users (id, email, unsub_nonce, created_at) VALUES (?1, ?2, ?3, ?4) ON CONFLICT (email) DO NOTHING")
      .bind(randomToken(16), used.email, randomToken(16), now),
    env.DB.prepare("SELECT id FROM users WHERE email = ?1").bind(used.email),
  ]);
  const userId = (user.results[0] as { id: string }).id;
  await env.DB.prepare("INSERT INTO preferences (user_id, updated_at) VALUES (?1, ?2) ON CONFLICT (user_id) DO NOTHING")
    .bind(userId, now)
    .run();

  const sessionId = await createSession(env, request, userId, now);
  return json({ ok: true, next: redirectPath(body.next) }, 200, { "Set-Cookie": sessionCookie(sessionId) });
});
