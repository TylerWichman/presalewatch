// POST /api/auth/request {email, turnstileToken, next?, follow?}: email a sign-in link and a 6-digit code.
// follow: the artist from an "Alert me" button, followed once this request signs the user in.
// The response is the same whether or not the address has an account (or was rate limited), and
// it always sets a pending-login cookie: the code only works in this browser.

import { randomToken } from "../../../src/lib/crypto.ts";
import { renderSignIn, sendEmail } from "../../../src/lib/email.ts";
import { clientIp, HttpError, json, log, nowSeconds, readJson, route, type Env } from "../../../src/lib/http.ts";
import { allow, LIMITS } from "../../../src/lib/ratelimit.ts";
import { createRequest, pendingCookie, pendingFollow, TOKEN_TTL } from "../../../src/lib/signin.ts";
import { verifyTurnstile } from "../../../src/lib/turnstile.ts";
import { email, redirectPath } from "../../../src/lib/validate.ts";

export { TOKEN_TTL };
export const SIGNIN_MESSAGE = "If that address can receive email, a sign-in link and code are on their way. They expire in 15 minutes.";

/** Store a request bound to `binding` and, if the address's limits allow, email it. Used by
 * request and resend. Sending happens in the background so timing doesn't reveal anything. */
export async function issue(env: Env, address: string, binding: string, next: string, now: number,
  waitUntil: (p: Promise<unknown>) => void, follow: string | null = null) {
  const allowed = (await allow(env, LIMITS.signinEmail, address, now)) && (await allow(env, LIMITS.signinGlobal, "all", now));
  const { token, code } = await createRequest(env, address, binding, now, follow);
  if (!allowed) {
    log("warn", { where: "auth/request", reason: "email or global limit" });
    return;
  }
  // The token rides in the fragment, which browsers never send to servers, so it stays out of logs.
  const link = `${env.APP_ORIGIN}/auth/confirm#token=${token}&next=${encodeURIComponent(next)}`;
  waitUntil(
    sendEmail(env, renderSignIn(env.EMAIL_FROM, address, link, code)).catch((err: unknown) =>
      log("error", { where: "auth/request", kind: err instanceof Error ? err.message : "send failed" }),
    ),
  );
}

export const onRequestPost = route(async ({ request, env, waitUntil }) => {
  const body = await readJson(request);
  const ip = clientIp(request);
  const now = nowSeconds();
  if (!(await allow(env, LIMITS.signinIp, ip, now))) throw new HttpError(429, "Too many sign-in attempts. Try again later.");
  if (!(await verifyTurnstile(env, body.turnstileToken, ip))) throw new HttpError(400, "Verification failed. Please try again.");
  const address = email(body.email);
  const follow = pendingFollow(body.follow);
  const binding = randomToken(32);
  await issue(env, address, binding, redirectPath(body.next), now, waitUntil, follow);
  return json({ ok: true, message: SIGNIN_MESSAGE }, 200, { "Set-Cookie": pendingCookie(binding) });
});
