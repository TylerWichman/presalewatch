// Shared test data for alert matching, email, and Worker tests.

import type { AlertUser, FeedEvent } from "../../src/lib/match.ts";

export const NOW = new Date("2026-10-01T12:00:00Z");

export function ev(over: Partial<FeedEvent> = {}): FeedEvent {
  return {
    id: "E1", artist: "Some Artist", name: null, url: "https://www.ticketmaster.com/event/E1", venue: "Venue", city: "New York",
    state: "NY", date: "2026-12-01", presaleEnd: "2026-10-05T12:00:00Z", mode: "predicted", profit: null,
    profitLow: 0.22, profitHigh: 1.04, edge: 0.63, tier: "High", confidence: "Med", ...over,
  };
}

export function user(over: Partial<AlertUser> = {}): AlertUser {
  return { id: "U".repeat(22), email: "u@example.com", unsub_nonce: "n", follow_alerts: 1, profit_alerts: 1, profit_threshold: 30, ...over };
}

