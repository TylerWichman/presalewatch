// PUT /api/preferences {followAlerts, profitAlerts, profitThreshold}: update the signed-in user's settings.

import { json, nowSeconds, readJson, route } from "../../src/lib/http.ts";
import { requireSession } from "../../src/lib/session.ts";
import { bool, threshold } from "../../src/lib/validate.ts";
import { loadPreferences } from "./me.ts";

export const onRequestPut = route(async ({ request, env }) => {
  const now = nowSeconds();
  const session = await requireSession(env, request, now);
  const body = await readJson(request);
  const followAlerts = bool(body.followAlerts, "followAlerts");
  const profitAlerts = bool(body.profitAlerts, "profitAlerts");
  const profitThreshold = threshold(body.profitThreshold);
  await env.DB.prepare(
    `INSERT INTO preferences (user_id, follow_alerts, profit_alerts, profit_threshold, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5)
     ON CONFLICT (user_id) DO UPDATE SET follow_alerts = ?2, profit_alerts = ?3, profit_threshold = ?4, updated_at = ?5`,
  ).bind(session.userId, followAlerts ? 1 : 0, profitAlerts ? 1 : 0, profitThreshold, now).run();
  return json({ ok: true, preferences: await loadPreferences(env, session.userId) });
});
