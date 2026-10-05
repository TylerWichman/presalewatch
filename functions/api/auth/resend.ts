// POST /api/auth/resend {next?}: a fresh link and code for this browser's pending sign-in request.
// The old ones stop working. At most once every 30 seconds, and the sign-in rate limits still
// apply; the answer is the same whether or not an email was actually sent.

import { sha256 } from "../../../src/lib/crypto.ts";
import { clientIp, HttpError, json, nowSeconds, readJson, route } from "../../../src/lib/http.ts";
import { allow, LIMITS } from "../../../src/lib/ratelimit.ts";
import { pendingCookie, readPending, RESEND_WAIT } from "../../../src/lib/signin.ts";
import { redirectPath } from "../../../src/lib/validate.ts";
import { EXPIRED } from "./code.ts";
import { issue, SIGNIN_MESSAGE } from "./request.ts";

export const onRequestPost = route(async ({ request, env, waitUntil }) => {
  const body = await readJson(request);
  const now = nowSeconds();
  if (!(await allow(env, LIMITS.signinIp, clientIp(request), now))) throw new HttpError(429, "Too many sign-in attempts. Try again later.");
  const binding = readPending(request);
  if (!binding) return json({ error: EXPIRED, restart: true }, 400);
  const bindingHash = await sha256(binding);
  const row = await env.DB.prepare(
    "SELECT token_hash, email, created_at FROM login_tokens WHERE request_binding_hash = ?1 AND used_at IS NULL AND expires_at > ?2" +
      " ORDER BY created_at DESC LIMIT 1",
  ).bind(bindingHash, now).first<{ token_hash: string; email: string; created_at: number }>();
  if (!row) return json({ error: EXPIRED, restart: true }, 400);
  if (now - row.created_at < RESEND_WAIT) throw new HttpError(429, "Wait a few seconds before sending another email.");

  await env.DB.prepare("UPDATE login_tokens SET used_at = ?2 WHERE request_binding_hash = ?1 AND used_at IS NULL")
    .bind(bindingHash, now)
    .run();
  await issue(env, row.email, binding, redirectPath(body.next), now, waitUntil);
  return json({ ok: true, message: SIGNIN_MESSAGE }, 200, { "Set-Cookie": pendingCookie(binding) });
});
