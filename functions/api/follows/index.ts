// GET /api/follows: the signed-in user's followed artists.
// POST /api/follows {artist}: follow an artist (free text allowed). Following twice is a no-op.

import { HttpError, json, nowSeconds, readJson, route } from "../../../src/lib/http.ts";
import { requireSession } from "../../../src/lib/session.ts";
import { artist, artistKey, FOLLOWS_MAX, InputError } from "../../../src/lib/validate.ts";
import { loadFollows } from "../me.ts";

export const onRequestGet = route(async ({ request, env }) => {
  const session = await requireSession(env, request, nowSeconds());
  return json({ follows: await loadFollows(env, session.userId) });
});

export const onRequestPost = route(async ({ request, env }) => {
  const now = nowSeconds();
  const session = await requireSession(env, request, now);
  const body = await readJson(request);
  const name = artist(body.artist);
  const key = artistKey(name);
  if (!key) throw new InputError("Enter an artist name with letters or numbers.");

  const count = await env.DB.prepare("SELECT COUNT(*) AS n FROM follows WHERE user_id = ?1").bind(session.userId).first<{ n: number }>();
  if ((count?.n ?? 0) >= FOLLOWS_MAX) throw new HttpError(400, `You can follow up to ${FOLLOWS_MAX} artists.`);

  await env.DB.prepare(
    "INSERT INTO follows (user_id, artist, artist_key, created_at) VALUES (?1, ?2, ?3, ?4) ON CONFLICT (user_id, artist_key) DO NOTHING",
  ).bind(session.userId, name, key, now).run();
  const follow = await env.DB.prepare("SELECT id, artist FROM follows WHERE user_id = ?1 AND artist_key = ?2")
    .bind(session.userId, key)
    .first<{ id: number; artist: string }>();
  return json({ ok: true, follow }, 201);
});
