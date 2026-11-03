// PUT /api/preferences {followAlerts}: update the signed-in user's settings.

import { json, nowSeconds, readJson, route } from "../../src/lib/http.ts";
import { requireSession } from "../../src/lib/session.ts";
import { bool } from "../../src/lib/validate.ts";
import { loadPreferences } from "./me.ts";

export const onRequestPut = route(async ({ request, env }) => {
  const now = nowSeconds();
  const session = await requireSession(env, request, now);
  const body = await readJson(request);
  const followAlerts = bool(body.followAlerts, "followAlerts");
  await env.DB.prepare(
    `INSERT INTO preferences (user_id, follow_alerts, updated_at)
     VALUES (?1, ?2, ?3)
     ON CONFLICT (user_id) DO UPDATE SET follow_alerts = ?2, updated_at = ?3`,
  ).bind(session.userId, followAlerts ? 1 : 0, now).run();
  return json({ ok: true, preferences: await loadPreferences(env, session.userId) });
});
