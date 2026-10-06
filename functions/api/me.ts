// GET /api/me: the signed-in user's own settings (or signedIn: false).
// DELETE /api/me: delete the account and all of its data, and end every session.

import { json, nowSeconds, route, type Env } from "../../src/lib/http.ts";
import { clearCookie, getSession, requireSession } from "../../src/lib/session.ts";

export interface Preferences {
  followAlerts: boolean;
  profitAlerts: boolean;
  profitThreshold: number;
}

export async function loadPreferences(env: Env, userId: string): Promise<Preferences> {
  const p = await env.DB.prepare(
    "SELECT follow_alerts, profit_alerts, profit_threshold FROM preferences WHERE user_id = ?1",
  ).bind(userId).first<{ follow_alerts: number; profit_alerts: number; profit_threshold: number }>();
  return {
    followAlerts: Boolean(p?.follow_alerts ?? 1),
    profitAlerts: Boolean(p?.profit_alerts ?? 1),
    profitThreshold: p?.profit_threshold ?? 30,
  };
}

export async function loadFollows(env: Env, userId: string): Promise<{ id: number; artist: string }[]> {
  const rows = await env.DB.prepare("SELECT id, artist FROM follows WHERE user_id = ?1 ORDER BY artist COLLATE NOCASE")
    .bind(userId)
    .all<{ id: number; artist: string }>();
  return rows.results;
}

export const onRequestGet = route(async ({ request, env }) => {
  const session = await getSession(env, request, nowSeconds());
  if (!session) return json({ signedIn: false });
  const user = await env.DB.prepare("SELECT email FROM users WHERE id = ?1").bind(session.userId).first<{ email: string }>();
  if (!user) return json({ signedIn: false });
  return json(
    {
      signedIn: true,
      email: user.email,
      preferences: await loadPreferences(env, session.userId),
      follows: await loadFollows(env, session.userId),
    },
    200,
    // Each visit extends the session; the cookie's lifetime follows.
    session.renewedCookie ? { "Set-Cookie": session.renewedCookie } : undefined,
  );
});

export const onRequestDelete = route(async ({ request, env }) => {
  const session = await requireSession(env, request, nowSeconds());
  const user = await env.DB.prepare("SELECT email FROM users WHERE id = ?1").bind(session.userId).first<{ email: string }>();
  const id = session.userId;
  // Explicit deletes (not only ON DELETE CASCADE) so nothing is left behind even if FKs were off.
  await env.DB.batch([
    env.DB.prepare("DELETE FROM sessions WHERE user_id = ?1").bind(id),
    env.DB.prepare("DELETE FROM follows WHERE user_id = ?1").bind(id),
    env.DB.prepare("DELETE FROM preferences WHERE user_id = ?1").bind(id),
    env.DB.prepare("DELETE FROM sent_alerts WHERE user_id = ?1").bind(id),
    env.DB.prepare("DELETE FROM login_tokens WHERE email = ?1").bind(user?.email ?? ""),
    env.DB.prepare("DELETE FROM users WHERE id = ?1").bind(id),
  ]);
  return json({ ok: true }, 200, { "Set-Cookie": clearCookie() });
});
