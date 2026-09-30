// POST /api/auth/request {email, turnstileToken, next?}: email a magic sign-in link.
// The response is the same whether or not the address has an account (or was rate limited).

import { randomToken, sha256 } from "../../../src/lib/crypto.ts";
import { renderSignIn, sendEmail } from "../../../src/lib/email.ts";
import { clientIp, HttpError, json, log, nowSeconds, readJson, route } from "../../../src/lib/http.ts";
import { allow, LIMITS } from "../../../src/lib/ratelimit.ts";
import { verifyTurnstile } from "../../../src/lib/turnstile.ts";
import { email, redirectPath } from "../../../src/lib/validate.ts";

export const TOKEN_TTL = 15 * 60;
export const SIGNIN_MESSAGE = "If that address can receive email, a sign-in link is on its way. It expires in 15 minutes.";

export const onRequestPost = route(async ({ request, env, waitUntil }) => {
  const body = await readJson(request);
  const ip = clientIp(request);
  const now = nowSeconds();
  if (!(await allow(env, LIMITS.signinIp, ip, now))) throw new HttpError(429, "Too many sign-in attempts. Try again later.");
  if (!(await verifyTurnstile(env, body.turnstileToken, ip))) throw new HttpError(400, "Verification failed. Please try again.");
  const address = email(body.email);
  const next = redirectPath(body.next);

  const allowed = (await allow(env, LIMITS.signinEmail, address, now)) && (await allow(env, LIMITS.signinGlobal, "all", now));
  if (allowed) {
    const token = randomToken(32);
    await env.DB.prepare("INSERT INTO login_tokens (token_hash, email, expires_at) VALUES (?1, ?2, ?3)")
      .bind(await sha256(token), address, now + TOKEN_TTL)
      .run();
    // The token rides in the fragment, which browsers never send to servers, so it stays out of logs.
    const link = `${env.APP_ORIGIN}/auth/confirm#token=${token}&next=${encodeURIComponent(next)}`;
    // Sent in the background so response time doesn't depend on the send.
    waitUntil(
      sendEmail(env, renderSignIn(env.EMAIL_FROM, address, link)).catch((err: unknown) =>
        log("error", { where: "auth/request", kind: err instanceof Error ? err.message : "send failed" }),
      ),
    );
  } else {
    log("warn", { where: "auth/request", reason: "email or global limit" });
  }
  return json({ ok: true, message: SIGNIN_MESSAGE });
});
