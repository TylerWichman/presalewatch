import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import { run, type WorkerEnv } from "../../worker/src/index.ts";
import { fakeFetch, makeEnv, signIn } from "./helpers.ts";
import { NOW } from "./fixtures.ts";

function feed(generatedAt: string, events: Record<string, unknown>[]) {
  return { v: 1, generated_at: generatedAt, events };
}
const HOT = { id: "HOT", artist: "Hot Act", mode: "live", profit: 0.8, date: "2026-12-01", presale_end: "2026-10-05T00:00:00Z", url: "https://www.ticketmaster.com/e/HOT" };
const FAV = { id: "FAV", artist: "Fave Band", mode: "predicted", profit_low: -0.2, profit_high: 0.2, tier: "Low", date: "2026-12-02", presale_end: "2026-10-05T00:00:00Z" };

let base: ReturnType<typeof makeEnv>;
let env: WorkerEnv;
let net: ReturnType<typeof fakeFetch>;
let logs: string[];
const realLog = console.log;

async function setup(feedBody: unknown, over: Partial<WorkerEnv> = {}, resendStatus = 200) {
  net?.restore();
  net = fakeFetch({ feed: feedBody, resendStatus });
  env = {
    DB: base.DB, APP_ORIGIN: base.APP_ORIGIN, EMAIL_FROM: base.EMAIL_FROM, RESEND_API_KEY: base.RESEND_API_KEY,
    UNSUBSCRIBE_SECRET: base.UNSUBSCRIBE_SECRET, POSTAL_ADDRESS: "PO Box 1, Town, ST 00000", DRY_RUN: "false", MAX_EMAILS_PER_RUN: "80", ...over,
  };
}

beforeEach(async () => {
  base = makeEnv();
  net = fakeFetch();
  await signIn(base, net.mail, "alice@example.com");
  await signIn(base, net.mail, "bob@example.com");
  const [bob] = base.DB.rows<{ id: string }>("SELECT id FROM users WHERE email = 'bob@example.com'");
  base.DB.sqlite.prepare("INSERT INTO follows (user_id, artist, artist_key, created_at) VALUES (?, 'Fave Band', 'fave band', 0)").run(bob.id);
  logs = [];
  console.log = (msg: string) => logs.push(String(msg));
});
afterEach(() => {
  console.log = realLog;
  net.restore();
});

const batches = () => net.mail.filter((m) => m.url.endsWith("/emails/batch"));

describe("alert worker", () => {
  it("sends one digest per user with matching events", async () => {
    await setup(feed("g1", [HOT, FAV]));
    const res = await run(env, NOW);
    assert.equal(res.status, "complete");
    const [batch] = batches();
    const byTo = Object.fromEntries(batch.body.map((m: { to: string[]; text: string }) => [m.to[0], m.text]));
    assert.deepEqual(Object.keys(byTo).sort(), ["alice@example.com", "bob@example.com"]);
    assert.match(byTo["alice@example.com"], /Hot Act/);
    assert.doesNotMatch(byTo["alice@example.com"], /Fave Band/);
    assert.match(byTo["bob@example.com"], /ARTISTS YOU FOLLOW[\s\S]*Fave Band/);
  });

  it("never sends the same event to the same user twice", async () => {
    await setup(feed("g1", [HOT]));
    await run(env, NOW);
    await setup(feed("g2", [HOT]));
    const second = await run(env, NOW);
    assert.equal(second.digests, 0);
    assert.equal(batches().length, 0);
    await setup(feed("g3", [HOT, { ...HOT, id: "HOT2" }]));
    await run(env, NOW);
    const texts = batches()[0].body.map((m: { text: string }) => m.text).join("\n");
    assert.equal((texts.match(/Hot Act/g) ?? []).length, 2, "only the new event, once per user");
  });

  it("does nothing when the data hasn't changed since the last run", async () => {
    await setup(feed("g1", [HOT]));
    await run(env, NOW);
    await setup(feed("g1", [HOT, { ...HOT, id: "NEW" }]));
    assert.equal((await run(env, NOW)).status, "no-new-data");
    assert.equal(batches().length, 0);
  });

  it("rolls back and retries next run when sending fails", async () => {
    await setup(feed("g1", [HOT]), {}, 500);
    const res = await run(env, NOW);
    assert.equal(res.status, "partial");
    assert.equal(base.DB.rows("SELECT * FROM sent_alerts").length, 0);
    await setup(feed("g1", [HOT]));
    const retry = await run(env, NOW);
    assert.equal(retry.status, "complete");
    assert.equal(retry.sent, 2);
    assert.equal(base.DB.rows("SELECT * FROM sent_alerts").length, 2);
  });

  it("dry run logs digests by user ID, with no addresses or tokens, and sends nothing", async () => {
    await setup(feed("g1", [HOT]), { DRY_RUN: "true" });
    const res = await run(env, NOW);
    assert.equal(res.sent, 2);
    assert.equal(batches().length, 0);
    assert.equal(base.DB.rows("SELECT * FROM sent_alerts").length, 0);
    const out = logs.join("\n");
    const ids = base.DB.rows<{ id: string }>("SELECT id FROM users").map((u) => u.id);
    for (const id of ids) assert.ok(out.includes(id));
    assert.doesNotMatch(out, /@example\.com/);
    assert.doesNotMatch(out, /[?&#]t=[A-Za-z0-9_-]{43}/);
    assert.match(out, /t=\[redacted\]/);
    // Switching dry run off afterwards still sends the same data for real.
    await setup(feed("g1", [HOT]));
    assert.equal((await run(env, NOW)).sent, 2);
  });

  it("refuses to send real email without a postal address", async () => {
    await setup(feed("g1", [HOT]), { POSTAL_ADDRESS: " " });
    assert.equal((await run(env, NOW)).status, "blocked");
    assert.equal(batches().length, 0);
  });

  it("caps emails per run and picks up the rest next run", async () => {
    await setup(feed("g1", [HOT]), { MAX_EMAILS_PER_RUN: "1" });
    assert.deepEqual(await run(env, NOW), { status: "partial", digests: 2, sent: 1 });
    await setup(feed("g1", [HOT]), { MAX_EMAILS_PER_RUN: "1" });
    assert.deepEqual(await run(env, NOW), { status: "complete", digests: 1, sent: 1 });
  });

  it("skips users who unsubscribed", async () => {
    base.DB.sqlite.exec("UPDATE preferences SET follow_alerts = 0, profit_alerts = 0");
    await setup(feed("g1", [HOT]));
    assert.equal((await run(env, NOW)).digests, 0);
  });

  it("cleans up expired tokens, sessions, and rate-limit rows", async () => {
    base.DB.sqlite.exec("INSERT INTO login_tokens (token_hash, email, expires_at) VALUES ('old', 'x@example.com', 1)");
    base.DB.sqlite.exec("INSERT INTO rate_limits (key, window_start, count) VALUES ('k', 1, 1)");
    base.DB.sqlite.exec("UPDATE sessions SET expires_at = 1");
    await setup(feed("g1", []));
    await run(env, NOW);
    assert.equal(base.DB.rows("SELECT * FROM login_tokens WHERE token_hash = 'old'").length, 0);
    assert.equal(base.DB.rows("SELECT * FROM rate_limits WHERE key = 'k'").length, 0);
    assert.equal(base.DB.rows("SELECT * FROM sessions").length, 0);
  });
});
