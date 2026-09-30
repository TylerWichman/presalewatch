import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { buildDigests, isHighProfit, parseFeed, PROFIT_CAP } from "../../src/lib/match.ts";
import { ev, NOW, user } from "./fixtures.ts";

describe("high profit rule", () => {
  it("uses Live Profit % when live data exists", () => {
    assert.equal(isHighProfit(ev({ mode: "live", profit: 0.3, tier: "Low" }), 30), true);
    assert.equal(isHighProfit(ev({ mode: "live", profit: 0.29, tier: "High" }), 30), false);
  });

  it("uses the estimate midpoint only for Med or High demand", () => {
    assert.equal(isHighProfit(ev({ profitLow: 0.2, profitHigh: 0.4, tier: "Med" }), 30), true);
    assert.equal(isHighProfit(ev({ profitLow: 0.2, profitHigh: 0.4, tier: "High" }), 30), true);
    assert.equal(isHighProfit(ev({ profitLow: 0.2, profitHigh: 0.4, tier: "Low" }), 30), false);
    assert.equal(isHighProfit(ev({ profitLow: 0.1, profitHigh: 0.4, tier: "High" }), 30), false);
  });

  it("the Med tier's range (-18% to +22%) doesn't reach the 30% default", () => {
    assert.equal(isHighProfit(ev({ profitLow: -0.184, profitHigh: 0.224, tier: "Med" }), 30), false);
    assert.equal(isHighProfit(ev({ profitLow: -0.184, profitHigh: 0.224, tier: "Med" }), 2), true);
  });
});

describe("digests", () => {
  const none = new Map<string, Set<string>>();

  it("matches followed artists regardless of accents, case, and punctuation", () => {
    const events = [ev({ id: "A", artist: "Beyoncé", tier: "Low" }), ev({ id: "B", artist: "Other", tier: "Low" })];
    const follows = new Map([[user().id, new Set(["beyonce"])]]);
    const [d] = buildDigests(events, [user()], follows, none, NOW);
    assert.deepEqual(d.follows.map((e) => e.id), ["A"]);
    assert.deepEqual(d.profit, []);
  });

  it("puts each event in one section only", () => {
    const events = [ev({ id: "A", artist: "Big Star" })];
    const follows = new Map([[user().id, new Set(["big star"])]]);
    const [d] = buildDigests(events, [user()], follows, none, NOW);
    assert.deepEqual(d.follows.map((e) => e.id), ["A"]);
    assert.deepEqual(d.profit, []);
  });

  it("sorts high-profit events by profit, highest first, and caps them at 25", () => {
    const events = Array.from({ length: 40 }, (_, i) => ev({ id: `E${i}`, mode: "live", profit: 0.3 + i / 100 }));
    const [d] = buildDigests(events, [user()], none, none, NOW);
    assert.equal(d.profit.length, PROFIT_CAP);
    assert.equal(d.profit[0].id, "E39");
    for (let i = 1; i < d.profit.length; i++) assert.ok((d.profit[i - 1].profit ?? 0) >= (d.profit[i].profit ?? 0));
  });

  it("never includes events already sent to that user", () => {
    const events = [ev({ id: "A" }), ev({ id: "B" })];
    const sent = new Map([[user().id, new Set(["A"])]]);
    const [d] = buildDigests(events, [user()], none, sent, NOW);
    assert.deepEqual(d.profit.map((e) => e.id), ["B"]);
    const again = buildDigests(events, [user()], none, new Map([[user().id, new Set(["A", "B"])]]), NOW);
    assert.deepEqual(again, []);
  });

  it("respects each alert type's on/off switch and the user's threshold", () => {
    const events = [ev({ id: "A", artist: "Fav", tier: "Low" }), ev({ id: "B", mode: "live", profit: 0.5 })];
    const follows = new Map([[user().id, new Set(["fav"])]]);
    assert.deepEqual(buildDigests(events, [user({ follow_alerts: 0, profit_alerts: 0 })], follows, none, NOW), []);
    const [onlyProfit] = buildDigests(events, [user({ follow_alerts: 0 })], follows, none, NOW);
    assert.deepEqual([onlyProfit.follows.length, onlyProfit.profit.map((e) => e.id)], [0, ["B"]]);
    assert.deepEqual(buildDigests(events, [user({ follow_alerts: 0, profit_threshold: 60 })], follows, none, NOW), []);
  });

  it("skips events whose presales have closed", () => {
    const events = [ev({ id: "Old", presaleEnd: "2026-09-30T00:00:00Z" }), ev({ id: "New" })];
    const [d] = buildDigests(events, [user()], none, none, NOW);
    assert.deepEqual(d.profit.map((e) => e.id), ["New"]);
  });

  it("keeps users separate", () => {
    const a = user({ id: "A".repeat(22) });
    const b = user({ id: "B".repeat(22), profit_alerts: 0 });
    const follows = new Map([[b.id, new Set(["some artist"])]]);
    const digests = buildDigests([ev()], [a, b], follows, new Map([[a.id, new Set<string>()]]), NOW);
    assert.equal(digests.length, 2);
    assert.deepEqual(digests.map((d) => [d.user.id, d.profit.length, d.follows.length]), [[a.id, 1, 0], [b.id, 0, 1]]);
  });
});

describe("feed parsing", () => {
  it("drops malformed events and strips control characters", () => {
    const feed = parseFeed({
      v: 1, generated_at: "2026-10-01T00:00:00Z",
      events: [
        { id: "ok", artist: "Fine\u0007 Artist", mode: "live", profit: 0.5 },
        { id: "bad id!", artist: "X", mode: "live" },
        { id: "nomode", artist: "X" },
        { id: "noartist", mode: "live" },
        { id: "inf", artist: "X", mode: "live", profit: 1e308 },
        "junk",
      ],
    });
    assert.deepEqual(feed.events.map((e) => e.id), ["ok", "inf"]);
    assert.equal(feed.events[0].artist, "Fine Artist");
    assert.equal(feed.events[1].profit, null);
  });

  it("rejects a feed with the wrong shape", () => {
    assert.throws(() => parseFeed({ v: 2, generated_at: "x", events: [] }));
    assert.throws(() => parseFeed(null));
  });
});
