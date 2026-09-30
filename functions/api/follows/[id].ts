// DELETE /api/follows/:id: unfollow. Only matches the signed-in user's own rows.

import { HttpError, json, nowSeconds, route } from "../../../src/lib/http.ts";
import { requireSession } from "../../../src/lib/session.ts";
import { followId } from "../../../src/lib/validate.ts";

export const onRequestDelete = route(async ({ request, env, params }) => {
  const session = await requireSession(env, request, nowSeconds());
  const id = followId(typeof params.id === "string" ? params.id : undefined);
  const res = await env.DB.prepare("DELETE FROM follows WHERE id = ?1 AND user_id = ?2").bind(id, session.userId).run();
  // Someone else's follow looks exactly like one that doesn't exist.
  if (!res.meta.changes) throw new HttpError(404, "Not found.");
  return json({ ok: true });
});
