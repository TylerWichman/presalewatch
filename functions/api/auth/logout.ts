// POST /api/auth/logout: revoke the session server-side and clear the cookie.

import { json, nowSeconds, route } from "../../../src/lib/http.ts";
import { clearCookie, getSession } from "../../../src/lib/session.ts";

export const onRequestPost = route(async ({ request, env }) => {
  const session = await getSession(env, request, nowSeconds());
  if (session) await env.DB.prepare("DELETE FROM sessions WHERE id_hash = ?1").bind(session.idHash).run();
  return json({ ok: true }, 200, { "Set-Cookie": clearCookie() });
});
