// Server-side Cloudflare Turnstile check.

import type { Env } from "./http.ts";

const VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify";
export const SIGNIN_ACTION = "signin";

export async function verifyTurnstile(env: Env, token: unknown, ip: string): Promise<boolean> {
  if (typeof token !== "string" || !token || token.length > 2048) return false;
  const body = new FormData();
  body.set("secret", env.TURNSTILE_SECRET);
  body.set("response", token);
  if (ip !== "unknown") body.set("remoteip", ip);
  const res = await fetch(VERIFY_URL, { method: "POST", body });
  if (!res.ok) return false;
  const out = (await res.json()) as { success?: boolean; hostname?: string; action?: string };
  return out.success === true && out.hostname === new URL(env.APP_ORIGIN).hostname && out.action === SIGNIN_ACTION;
}
