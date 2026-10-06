// POST /api/auth/code {code, next?}: sign in with the 6-digit code from the email.
// Only works in the browser that requested it (pending-login cookie). Each try is counted before
// the code is compared, atomically, so at most 5 guesses are ever checked per request, even in
// parallel. A right code uses up the request (and its link); after 5 wrong ones it's dead.

import { sha256, timingSafeEqual } from "../../../src/lib/crypto.ts";
import { clientIp, HttpError, json, nowSeconds, readJson, route } from "../../../src/lib/http.ts";
import { allow, LIMITS } from "../../../src/lib/ratelimit.ts";
import { sessionCookie } from "../../../src/lib/session.ts";
import { clearPendingCookie, CODE_ATTEMPTS, CODE_PATTERN, codeHash, completeSignIn, readPending } from "../../../src/lib/signin.ts";
import { redirectPath } from "../../../src/lib/validate.ts";

export const EXPIRED = "This code has expired. Start over to get a new one.";

export const onRequestPost = route(async ({ request, env, waitUntil }) => {
  const body = await readJson(request);
  const now = nowSeconds();
  if (!(await allow(env, LIMITS.codeIp, clientIp(request), now))) throw new HttpError(429, "Too many attempts. Try again later.");
  const binding = readPending(request);
  if (!binding) return json({ error: EXPIRED, restart: true }, 400);
  if (typeof body.code !== "string" || !CODE_PATTERN.test(body.code)) throw new HttpError(400, "Enter the 6-digit code from the email.");

  // Reserve one attempt first; only requests still under the limit get a row back.
  const row = await env.DB.prepare(
    "UPDATE login_tokens SET attempts = attempts + 1" +
      " WHERE token_hash = (SELECT token_hash FROM login_tokens WHERE request_binding_hash = ?1 AND used_at IS NULL" +
      " AND expires_at > ?2 AND attempts < ?3 ORDER BY created_at DESC LIMIT 1)" +
      " RETURNING token_hash, email, code_hash, attempts",
  ).bind(await sha256(binding), now, CODE_ATTEMPTS).first<{ token_hash: string; email: string; code_hash: string; attempts: number }>();
  if (!row) return json({ error: EXPIRED, restart: true }, 400);
  if (!(await allow(env, LIMITS.codeEmail, row.email, now))) throw new HttpError(429, "Too many attempts. Try again later.");

  if (!timingSafeEqual(await codeHash(env, row.token_hash, body.code), row.code_hash)) {
    const left = CODE_ATTEMPTS - row.attempts;
    if (left <= 0) return json({ error: "Too many wrong codes. Start over to get a new one.", restart: true, attemptsLeft: 0 }, 400);
    return json({ error: `That code isn't right. ${left} ${left === 1 ? "try" : "tries"} left.`, attemptsLeft: left }, 400);
  }

  // Atomic single use, shared with the link: only one of them can flip used_at.
  const used = await env.DB.prepare(
    "UPDATE login_tokens SET used_at = ?2 WHERE token_hash = ?1 AND used_at IS NULL AND expires_at > ?2 RETURNING email, pending_follow",
  ).bind(row.token_hash, now).first<{ email: string; pending_follow: string | null }>();
  if (!used) return json({ error: EXPIRED, restart: true }, 400);

  const sessionId = await completeSignIn(env, request, used.email, now, { follow: used.pending_follow, waitUntil });
  return json({ ok: true, next: redirectPath(body.next) }, 200, [
    ["Set-Cookie", sessionCookie(sessionId)],
    ["Set-Cookie", clearPendingCookie()],
  ]);
});
