import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { buildDigests, FOLLOW_CAP, parseFeed } from "../../src/lib/match.ts";
import { ev, NOW, user } from "./fixtures.ts";

describe("digests", () => {
  const none = new Map<string, Set<string>>();

  it("matches followed artists regardless of accents, case, and punctuation", () => {
    const events = [ev({ id: "A", artist: "Beyoncé" }), ev({ id: "B", artist: "Other" })];
    const follows = new Map([[user().id, new Set(["beyonce"])]]);
    const [d] = buildDigests(events, [user()], follows, none, NOW);
    assert.deepEqual(d.follows.map((e) => e.id), ["A"]);
  });

  it("sends nothing for artists the user doesn't follow", () => {
    assert.deepEqual(buildDigests([ev()], [user()], none, none, NOW), []);
  });

  it("sorts by show date and caps the digest", () => {
    const events = Array.from({ length: FOLLOW_CAP + 10 }, (_, i) => ev({ id: `E${i}`, date: `2027-${String(12 - (i % 12)).padStart(2, "0")}-01` }));
    const follows = new Map([[user().id, new Set(["some artist"])]]);
    const [d] = buildDigests(events, [user()], follows, none, NOW);
    assert.equal(d.follows.length, FOLLOW_CAP);
    for (let i = 1; i < d.follows.length; i++) assert.ok((d.follows[i - 1].date ?? "") <= (d.follows[i].date ?? ""));
  });

  it("never includes events already sent to that user", () => {
    const events = [ev({ id: "A" }), ev({ id: "B" })];
    const follows = new Map([[user().id, new Set(["some artist"])]]);
    const [d] = buildDigests(events, [user()], follows, new Map([[user().id, new Set(["A"])]]), NOW);
    assert.deepEqual(d.follows.map((e) => e.id), ["B"]);
    assert.deepEqual(buildDigests(events, [user()], follows, new Map([[user().id, new Set(["A", "B"])]]), NOW), []);
  });

  it("respects the on/off switch", () => {
    const follows = new Map([[user().id, new Set(["some artist"])]]);
    assert.deepEqual(buildDigests([ev()], [user({ follow_alerts: 0 })], follows, none, NOW), []);
  });

  it("skips events whose presales have closed", () => {
    const events = [ev({ id: "Old", presaleEnd: "2026-09-30T00:00:00Z" }), ev({ id: "New" })];
    const follows = new Map([[user().id, new Set(["some artist"])]]);
    const [d] = buildDigests(events, [user()], follows, none, NOW);
    assert.deepEqual(d.follows.map((e) => e.id), ["New"]);
  });

  it("keeps users separate", () => {
    const a = user({ id: "A".repeat(22) });
    const b = user({ id: "B".repeat(22) });
    const follows = new Map([[b.id, new Set(["some artist"])]]);
    const digests = buildDigests([ev()], [a, b], follows, none, NOW);
    assert.deepEqual(digests.map((d) => [d.user.id, d.follows.length]), [[b.id, 1]]);
  });
});

describe("feed parsing", () => {
  it("drops malformed events and strips control characters", () => {
    const feed = parseFeed({
      v: 1, generated_at: "2026-10-01T00:00:00Z",
      events: [
        { id: "ok", artist: "Fine\u0007 Artist" },
        { id: "bad id!", artist: "X" },
        { id: "noartist" },
        "junk",
      ],
    });
    assert.deepEqual(feed.events.map((e) => e.id), ["ok"]);
    assert.equal(feed.events[0].artist, "Fine Artist");
  });

  it("rejects a feed with the wrong shape", () => {
    assert.throws(() => parseFeed({ v: 2, generated_at: "x", events: [] }));
    assert.throws(() => parseFeed(null));
  });
});
