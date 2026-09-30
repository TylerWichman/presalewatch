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
  mode: "live" | "predicted";
  profit: number | null; // Live Profit %, as a fraction
  profitLow: number | null;
  profitHigh: number | null;
  edge: number | null; // Live profit, or the midpoint of the estimated range
  tier: "High" | "Med" | "Low" | null;
  confidence: string | null;
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
  profit_alerts: number;
  profit_threshold: number; // whole percent
}

export interface Digest {
  user: AlertUser;
  profit: FeedEvent[];
  follows: FeedEvent[];
}

export const PROFIT_CAP = 25;
export const FOLLOW_CAP = 50;

// ---- Feed parsing: alerts.json is third-party data, so every field is checked. ----

function str(v: unknown, max: number): string | null {
  if (typeof v !== "string") return null;
  const s = v.replace(/[\p{Cc}\p{Cf}]/gu, "").trim();
  return s ? s.slice(0, max) : null;
}

function fraction(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) && Math.abs(v) < 1000 ? v : null;
}

function parseEvent(raw: unknown): FeedEvent | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const id = typeof r.id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(r.id) ? r.id : null;
  const artist = str(r.artist, 200);
  const mode = r.mode === "live" || r.mode === "predicted" ? r.mode : null;
  if (!id || !artist || !mode) return null;
  const tier = r.tier === "High" || r.tier === "Med" || r.tier === "Low" ? r.tier : null;
  const date = typeof r.date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(r.date) ? r.date : null;
  const presaleEnd = typeof r.presale_end === "string" && !Number.isNaN(Date.parse(r.presale_end)) ? r.presale_end : null;
  return {
    id, artist, mode, tier, date, presaleEnd,
    name: str(r.name, 200),
    url: str(r.url, 2000),
    venue: str(r.venue, 200),
    city: str(r.city, 100),
    state: str(r.state, 40),
    profit: fraction(r.profit),
    profitLow: fraction(r.profit_low),
    profitHigh: fraction(r.profit_high),
    edge: fraction(r.edge),
    confidence: str(r.confidence, 20),
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
 * High profit: Live Profit % at or above the threshold, or, for events without Live data,
 * an estimated range whose midpoint is at or above it with Med or High demand.
 */
export function isHighProfit(ev: FeedEvent, thresholdPct: number): boolean {
  const cutoff = thresholdPct / 100 - 1e-9;
  if (ev.mode === "live") return ev.profit !== null && ev.profit >= cutoff;
  if (ev.tier !== "Med" && ev.tier !== "High") return false;
  if (ev.profitLow === null || ev.profitHigh === null) return false;
  return (ev.profitLow + ev.profitHigh) / 2 >= cutoff;
}

export function edgeOf(ev: FeedEvent): number {
  if (ev.mode === "live") return ev.profit ?? -Infinity;
  return ev.profitLow !== null && ev.profitHigh !== null ? (ev.profitLow + ev.profitHigh) / 2 : -Infinity;
}

/**
 * One digest per user with anything new. An event appears once per digest (under "Artists you follow"
 * if followed, otherwise under "High profit"), and never if it's in sentByUser.
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
    const followed: FeedEvent[] = [];
    const profit: FeedEvent[] = [];
    for (const { e, key } of keyed) {
      if (sent.has(e.id)) continue;
      if (user.follow_alerts && follows.has(key)) followed.push(e);
      else if (user.profit_alerts && isHighProfit(e, user.profit_threshold)) profit.push(e);
    }
    profit.sort((a, b) => edgeOf(b) - edgeOf(a) || byDate(a, b));
    followed.sort(byDate);
    if (profit.length || followed.length) {
      digests.push({ user, profit: profit.slice(0, PROFIT_CAP), follows: followed.slice(0, FOLLOW_CAP) });
    }
  }
  return digests;
}
