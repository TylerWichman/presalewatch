// POST /api/auth/verify {token, confirm, next?}: the emailed sign-in link.
// confirm=false: check the link and return the address it's for. Nothing is used up: opening the
// link (by a person or by a mail scanner) never signs anyone in.
// confirm=true: the "Sign in" button. Uses up the request (single use, shared with its code),
// creates the account if new, and starts a session. Same CSRF and Origin checks as every POST.

import { sha256, TOKEN_PATTERN } from "../../../src/lib/crypto.ts";
import { clientIp, HttpError, json, nowSeconds, readJson, route } from "../../../src/lib/http.ts";
import { allow, LIMITS } from "../../../src/lib/ratelimit.ts";
import { sessionCookie } from "../../../src/lib/session.ts";
import { clearPendingCookie, completeSignIn } from "../../../src/lib/signin.ts";
import { redirectPath } from "../../../src/lib/validate.ts";

export const INVALID = "This link has expired. Request a new one.";

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
    // Whoever holds the link can sign in as this address, so showing it reveals nothing more.
    return json({ ok: true, email: row.email });
  }

  // Atomic single use: only one request can flip used_at from NULL.
  const used = await env.DB.prepare(
    "UPDATE login_tokens SET used_at = ?2 WHERE token_hash = ?1 AND used_at IS NULL AND expires_at > ?2 RETURNING email",
  ).bind(tokenHash, now).first<{ email: string }>();
  if (!used) throw new HttpError(400, INVALID);

  const sessionId = await completeSignIn(env, request, used.email, now);
  return json({ ok: true, next: redirectPath(body.next) }, 200, [
    ["Set-Cookie", sessionCookie(sessionId)],
    ["Set-Cookie", clearPendingCookie()],
  ]);
});
