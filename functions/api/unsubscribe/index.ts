// POST /api/unsubscribe {u, t}: from the unsubscribe page. No sign-in needed; the signed token is the proof.

import { clientIp, HttpError, json, nowSeconds, readJson, route, type Env } from "../../../src/lib/http.ts";
import { allow, LIMITS } from "../../../src/lib/ratelimit.ts";
import { checkUnsubscribeToken, UNSUB_TOKEN_PATTERN, USER_ID_PATTERN } from "../../../src/lib/unsubscribe.ts";
import { token } from "../../../src/lib/validate.ts";

const INVALID = "This unsubscribe link is invalid. Sign in to manage your alerts instead.";

/** Turn off every alert for the token's user, immediately. Throws a 400 for a bad link. */
export async function unsubscribe(env: Env, request: Request, userId: unknown, tok: unknown): Promise<void> {
  if (!(await allow(env, LIMITS.unsubscribeIp, clientIp(request), nowSeconds()))) throw new HttpError(429, "Too many attempts. Try again later.");
  const u = token(userId, USER_ID_PATTERN);
  const t = token(tok, UNSUB_TOKEN_PATTERN);
  const user = await env.DB.prepare("SELECT unsub_nonce FROM users WHERE id = ?1").bind(u).first<{ unsub_nonce: string }>();
  // Compute a signature even for unknown users so both cases take the same path.
  const ok = await checkUnsubscribeToken(env.UNSUBSCRIBE_SECRET, u, user?.unsub_nonce ?? "none", t);
  if (!user || !ok) throw new HttpError(400, INVALID);
  await env.DB.prepare("UPDATE preferences SET follow_alerts = 0, profit_alerts = 0, updated_at = ?2 WHERE user_id = ?1")
    .bind(u, nowSeconds())
    .run();
}

export const onRequestPost = route(async ({ request, env }) => {
  const body = await readJson(request);
  await unsubscribe(env, request, body.u, body.t);
  return json({ ok: true });
});
