// POST /api/unsubscribe/one-click?u=...&t=...: RFC 8058 one-click unsubscribe from the
// List-Unsubscribe header. Mail providers send this without an Origin header, so the middleware
// skips the CSRF check for this one path; the signed token authorizes it and it can only unsubscribe.

import { json, route } from "../../../src/lib/http.ts";
import { unsubscribe } from "./index.ts";

export const onRequestPost = route(async ({ request, env }) => {
  const url = new URL(request.url);
  await unsubscribe(env, request, url.searchParams.get("u"), url.searchParams.get("t"));
  return json({ ok: true });
});
