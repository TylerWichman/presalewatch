import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import { onRequestDelete as unfollow } from "../../functions/api/follows/[id].ts";
import { onRequestGet as listFollows, onRequestPost as follow } from "../../functions/api/follows/index.ts";
import { onRequestDelete as deleteMe, onRequestGet as me } from "../../functions/api/me.ts";
import { onRequestPut as putPrefs } from "../../functions/api/preferences.ts";
import { FOLLOWS_MAX } from "../../src/lib/validate.ts";
import { call, fakeFetch, makeEnv, signIn } from "./helpers.ts";

let env: ReturnType<typeof makeEnv>;
let net: ReturnType<typeof fakeFetch>;
let alice: string;
let bob: string;

beforeEach(async () => {
  env = makeEnv();
  net = fakeFetch();
  alice = await signIn(env, net.mail, "alice@example.com");
  bob = await signIn(env, net.mail, "bob@example.com");
});
afterEach(() => net.restore());

async function followAs(cookie: string, artist: string) {
  const res = await call(follow, env, "/api/follows", { body: { artist }, cookie });
  return (await res.json()).follow as { id: number; artist: string };
}

const snapshot = (email: string) => {
  const [u] = env.DB.rows<{ id: string }>("SELECT id FROM users WHERE email = ?", email);
  return JSON.stringify({
    prefs: env.DB.rows("SELECT follow_alerts, profit_alerts, profit_threshold FROM preferences WHERE user_id = ?", u.id),
    follows: env.DB.rows("SELECT id, artist FROM follows WHERE user_id = ?", u.id),
    sessions: env.DB.rows("SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", u.id),
  });
};

describe("authorization", () => {
  it("every account endpoint requires a session", async () => {
    const cases: [typeof follow, string, string, unknown][] = [
      [listFollows, "GET", "/api/follows", undefined],
      [follow, "POST", "/api/follows", { artist: "X" }],
      [unfollow, "DELETE", "/api/follows/1", undefined],
      [putPrefs, "PUT", "/api/preferences", { followAlerts: true, profitAlerts: true, profitThreshold: 30 }],
      [deleteMe, "DELETE", "/api/me", undefined],
    ];
    for (const [handler, method, path, body] of cases) {
      const res = await call(handler, env, path, { method, body, params: { id: "1" } });
      assert.equal(res.status, 401, `${method} ${path}`);
    }
    assert.deepEqual(await (await call(me, env, "/api/me", { method: "GET" })).json(), { signedIn: false });
  });

  it("GET /api/me returns only the caller's own data", async () => {
    await followAs(bob, "Bob's Band");
    const res = await (await call(me, env, "/api/me", { method: "GET", cookie: alice })).json();
    assert.equal(res.email, "alice@example.com");
    assert.deepEqual(res.follows, []);
    assert.ok(!JSON.stringify(res).includes("bob"));
  });

  it("user A can't list, delete, or see user B's follows", async () => {
    const bobs = await followAs(bob, "Bob's Band");
    const before = snapshot("bob@example.com");
    const list = await (await call(listFollows, env, "/api/follows", { method: "GET", cookie: alice })).json();
    assert.deepEqual(list.follows, []);
    const res = await call(unfollow, env, `/api/follows/${bobs.id}`, { method: "DELETE", cookie: alice, params: { id: String(bobs.id) } });
    assert.equal(res.status, 404, "someone else's follow looks like a missing one");
    assert.equal(snapshot("bob@example.com"), before);
  });

  it("user A's preference changes never touch user B", async () => {
    const before = snapshot("bob@example.com");
    const res = await call(putPrefs, env, "/api/preferences", {
      method: "PUT", cookie: alice,
      body: { followAlerts: false, profitAlerts: false, profitThreshold: 5, user_id: "bob", userId: "bob" },
    });
    assert.equal(res.status, 200);
    assert.equal(snapshot("bob@example.com"), before);
    const mine = await (await call(me, env, "/api/me", { method: "GET", cookie: alice })).json();
    assert.deepEqual(mine.preferences, { followAlerts: false, profitAlerts: false, profitThreshold: 5 });
  });

  it("deleting user A removes all of A's data and nothing of B's", async () => {
    await followAs(alice, "Alice Artist");
    await followAs(bob, "Bob's Band");
    const [a] = env.DB.rows<{ id: string }>("SELECT id FROM users WHERE email = 'alice@example.com'");
    env.DB.sqlite.prepare("INSERT INTO sent_alerts (user_id, event_id, sent_at) VALUES (?, 'E1', 1)").run(a.id);
    const before = snapshot("bob@example.com");

    const res = await call(deleteMe, env, "/api/me", { method: "DELETE", cookie: alice });
    assert.equal(res.status, 200);
    assert.match(res.headers.get("Set-Cookie")!, /Max-Age=0/);
    for (const table of ["sessions", "follows", "preferences", "sent_alerts"]) {
      assert.equal(env.DB.rows(`SELECT * FROM ${table} WHERE user_id = ?`, a.id).length, 0, table);
    }
    assert.equal(env.DB.rows("SELECT * FROM users WHERE email = 'alice@example.com'").length, 0);
    assert.equal(env.DB.rows("SELECT * FROM login_tokens WHERE email = 'alice@example.com'").length, 0);
    assert.equal(snapshot("bob@example.com"), before);
    assert.equal((await (await call(me, env, "/api/me", { method: "GET", cookie: alice })).json()).signedIn, false);
  });
});

describe("input validation", () => {
  it("validates artist names and treats SQL-like input as plain text", async () => {
    for (const bad of ["", "   ", "x".repeat(101), "Bad\u0000Name", "RTL‮override", 42, null]) {
      const res = await call(follow, env, "/api/follows", { body: { artist: bad }, cookie: alice });
      assert.equal(res.status, 400, JSON.stringify(bad));
    }
    const sneaky = "Robert'); DROP TABLE users;--";
    const f = await followAs(alice, sneaky);
    assert.equal(f.artist, sneaky);
    assert.equal(env.DB.rows("SELECT * FROM users").length, 2);
  });

  it("following the same artist twice is a no-op, even with different spelling", async () => {
    const a = await followAs(alice, "Beyoncé");
    const b = await followAs(alice, "  BEYONCE ");
    assert.equal(a.id, b.id);
    assert.equal(env.DB.rows("SELECT * FROM follows").length, 1);
  });

  it("caps the number of follows", async () => {
    const [a] = env.DB.rows<{ id: string }>("SELECT id FROM users WHERE email = 'alice@example.com'");
    const ins = env.DB.sqlite.prepare("INSERT INTO follows (user_id, artist, artist_key, created_at) VALUES (?, ?, ?, 0)");
    for (let i = 0; i < FOLLOWS_MAX; i++) ins.run(a.id, `Artist ${i}`, `artist ${i}`);
    const res = await call(follow, env, "/api/follows", { body: { artist: "One More" }, cookie: alice });
    assert.equal(res.status, 400);
  });

  it("validates the profit threshold and booleans", async () => {
    for (const body of [
      { followAlerts: true, profitAlerts: true, profitThreshold: -1 },
      { followAlerts: true, profitAlerts: true, profitThreshold: 501 },
      { followAlerts: true, profitAlerts: true, profitThreshold: 12.5 },
      { followAlerts: true, profitAlerts: true, profitThreshold: "30" },
      { followAlerts: "yes", profitAlerts: true, profitThreshold: 30 },
      { profitThreshold: 30 },
    ]) {
      const res = await call(putPrefs, env, "/api/preferences", { method: "PUT", body, cookie: alice });
      assert.equal(res.status, 400, JSON.stringify(body));
    }
  });

  it("rejects oversized and malformed bodies", async () => {
    const big = await call(follow, env, "/api/follows", { body: JSON.stringify({ artist: "x".repeat(5000) }), cookie: alice });
    assert.equal(big.status, 413);
    const broken = await call(follow, env, "/api/follows", { body: "{not json", cookie: alice });
    assert.equal(broken.status, 400);
    const array = await call(follow, env, "/api/follows", { body: "[]", cookie: alice });
    assert.equal(array.status, 400);
  });

  it("rejects non-numeric follow IDs", async () => {
    for (const id of ["abc", "1 OR 1=1", "-1", "0", "1e3"]) {
      const res = await call(unfollow, env, "/api/follows/x", { method: "DELETE", cookie: alice, params: { id } });
      assert.equal(res.status, 400, id);
    }
  });
});
