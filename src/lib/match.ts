// Alert matching: which events go into each user's digest. Pure functions, no I/O.

import { artistKey } from "./validate.ts";

export interface FeedEvent {
  id: string;
  artist: string;
  name: string | null;
  url: string | null;
  venue: string | null;
  city: string | null;
  state: string | null;
  date: string | null; // YYYY-MM-DD, local to the venue
  presaleEnd: string | null; // ISO time the last presale closes
}

export interface Feed {
  generatedAt: string;
  events: FeedEvent[];
}

export interface AlertUser {
  id: string;
  email: string;
  unsub_nonce: string;
  follow_alerts: number;
}

export interface Digest {
  user: AlertUser;
  follows: FeedEvent[];
}

export const FOLLOW_CAP = 50;

// ---- Feed parsing: alerts.json is third-party data, so every field is checked. ----

function str(v: unknown, max: number): string | null {
  if (typeof v !== "string") return null;
  const s = v.replace(/[\p{Cc}\p{Cf}]/gu, "").trim();
  return s ? s.slice(0, max) : null;
}

function parseEvent(raw: unknown): FeedEvent | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const id = typeof r.id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(r.id) ? r.id : null;
  const artist = str(r.artist, 200);
  if (!id || !artist) return null;
  const date = typeof r.date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(r.date) ? r.date : null;
  const presaleEnd = typeof r.presale_end === "string" && !Number.isNaN(Date.parse(r.presale_end)) ? r.presale_end : null;
  return {
    id, artist, date, presaleEnd,
    name: str(r.name, 200),
    url: str(r.url, 2000),
    venue: str(r.venue, 200),
    city: str(r.city, 100),
    state: str(r.state, 40),
  };
}

export function parseFeed(raw: unknown): Feed {
  if (!raw || typeof raw !== "object") throw new Error("feed is not an object");
  const r = raw as Record<string, unknown>;
  if (r.v !== 1 || typeof r.generated_at !== "string" || !Array.isArray(r.events)) throw new Error("feed has the wrong shape");
  const events = r.events.map(parseEvent).filter((e): e is FeedEvent => e !== null);
  return { generatedAt: r.generated_at, events };
}

// ---- Matching ----

/** Still worth alerting: a presale hasn't closed yet (or, with no presale time, the show hasn't happened). */
export function isUpcoming(ev: FeedEvent, now: Date): boolean {
  if (ev.presaleEnd) return Date.parse(ev.presaleEnd) > now.getTime();
  return ev.date !== null && ev.date >= now.toISOString().slice(0, 10);
}

/**
 * One digest per user with anything new: upcoming events by artists they follow, never one that's
 * in sentByUser.
 */
export function buildDigests(
  events: FeedEvent[],
  users: AlertUser[],
  followsByUser: Map<string, Set<string>>,
  sentByUser: Map<string, Set<string>>,
  now: Date,
): Digest[] {
  const upcoming = events.filter((e) => isUpcoming(e, now));
  const keyed = upcoming.map((e) => ({ e, key: artistKey(e.artist) }));
  const byDate = (a: FeedEvent, b: FeedEvent) => (a.date ?? "").localeCompare(b.date ?? "") || a.artist.localeCompare(b.artist);
  const digests: Digest[] = [];
  for (const user of users) {
    const sent = sentByUser.get(user.id) ?? new Set<string>();
    const follows = followsByUser.get(user.id) ?? new Set<string>();
    if (!user.follow_alerts) continue;
    const followed: FeedEvent[] = [];
    for (const { e, key } of keyed) {
      if (!sent.has(e.id) && follows.has(key)) followed.push(e);
    }
    followed.sort(byDate);
    if (followed.length) digests.push({ user, follows: followed.slice(0, FOLLOW_CAP) });
  }
  return digests;
}
