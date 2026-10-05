import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, it } from "node:test";
import { onRequestPost as unsubscribePage } from "../../functions/api/unsubscribe/index.ts";
import { onRequestPost as oneClick } from "../../functions/api/unsubscribe/one-click.ts";
import { escapeHtml, renderDigest, renderSignIn, safeEventUrl } from "../../src/lib/email.ts";
import { unsubscribeToken } from "../../src/lib/unsubscribe.ts";
import { call, fakeFetch, makeEnv, signIn } from "./helpers.ts";
import { ev, user } from "./fixtures.ts";

const CTX = { origin: "https://pouchit.net", from: "PouchIt <alerts@pouchit.net>", unsubscribeSecret: "u".repeat(48), postalAddress: "PO Box 1, Town, ST 00000" };
const XSS = `<script>alert(1)</script><img src=x onerror=alert(2)>"'&`;

describe("deliverability", () => {
  const FROM = "PouchIt <alerts@pouchit.net>";

  it("both Cloudflare configs send from PouchIt <alerts@pouchit.net>", () => {
    for (const f of ["wrangler.toml", "worker/wrangler.toml"]) {
      const toml = readFileSync(new URL(`../../${f}`, import.meta.url), "utf8");
      assert.match(toml, /^EMAIL_FROM = "PouchIt <alerts@pouchit\.net>"$/m, f);
    }
  });

  it("every email has a plain-text part alongside the HTML", async () => {
    const digest = await renderDigest({ user: user(), follows: [ev({ id: "F" })], profit: [ev()] }, { ...CTX, from: FROM });
    const signin = renderSignIn(FROM, "a@example.com", "https://pouchit.net/auth/confirm#token=" + "A".repeat(43), "482913");
    for (const m of [digest, signin]) {
      assert.equal(m.from, FROM);
      assert.ok(m.text.trim().length > 0);
      assert.ok(m.html.trim().length > 0);
      assert.doesNotMatch(m.text, /<[a-z]/i, "text part has no HTML");
    }
  });

  it("the sign-in email is short and plain", () => {
    const link = "https://pouchit.net/auth/confirm#token=" + "A".repeat(43) + "&next=%2Falerts";
    const m = renderSignIn(FROM, "a@example.com", link, "482913");
    assert.ok(m.text.includes(link));
    assert.ok(m.text.includes("482913"));
    assert.throws(() => renderSignIn(FROM, "a@example.com", link, "48291"));
    assert.ok(m.text.length < 300, `text is ${m.text.length} characters`);
    assert.doesNotMatch(m.html, /<img|<style|style=|<table/i);
    assert.equal((m.html.match(/<a /g) ?? []).length, 1);
  });
});

describe("email escaping", () => {
  it("escapes artist, event, and venue names from third-party data", async () => {
    const msg = await renderDigest({ user: user(), follows: [], profit: [ev({ artist: XSS, name: XSS, venue: XSS, city: XSS })] }, CTX);
    assert.ok(!msg.html.includes("<script>"));
    assert.ok(!msg.html.includes("<img src=x"));
    assert.ok(msg.html.includes(escapeHtml("<script>alert(1)</script>")));
  });

  it("only links to https Ticketmaster or Live Nation pages", () => {
    assert.equal(safeEventUrl("https://www.ticketmaster.com/e/1"), "https://www.ticketmaster.com/e/1");
    assert.equal(safeEventUrl("https://concerts.livenation.com/e/1"), "https://concerts.livenation.com/e/1");
    for (const bad of ["javascript:alert(1)", "http://www.ticketmaster.com/e", "https://ticketmaster.com.evil.example/", "https://evil.example/?ticketmaster.com", "https://user:pw@www.ticketmaster.com/", "data:text/html,hi", "not a url"]) {
      assert.equal(safeEventUrl(bad), null, bad);
    }
  });

  it("replaces unsafe event links with the site link", async () => {
    const msg = await renderDigest({ user: user(), follows: [], profit: [ev({ url: "javascript:alert(1)" })] }, CTX);
    assert.ok(!msg.html.includes("javascript:"));
    assert.ok(msg.html.includes('href="https://pouchit.net/"'));
  });

  it("labels estimates as estimates and Live as asking-price based", async () => {
    const msg = await renderDigest({ user: user(), follows: [], profit: [ev(), ev({ id: "L", mode: "live", profit: 0.42 })] }, CTX);
    assert.match(msg.text, /Estimate: \+22% to \+104%/);
    assert.match(msg.text, /\+42% Profit %, based on asking prices/);
    assert.match(msg.text, /Items marked Estimate are predictions/);
  });

  it("shows unrated events (no artist listening data) as not rated", async () => {
    const msg = await renderDigest({ user: user(), follows: [ev({ tier: null, profitLow: null, profitHigh: null, edge: null })], profit: [] }, CTX);
    assert.match(msg.text, /Not rated: no artist listening data/);
    assert.ok(!msg.text.includes("Estimate: +0%"));
  });

  it("includes unsubscribe links, one-click headers, and the postal address", async () => {
    const msg = await renderDigest({ user: user(), follows: [], profit: [ev()] }, CTX);
    const token = await unsubscribeToken(CTX.unsubscribeSecret, user().id, user().unsub_nonce);
    assert.equal(msg.headers!["List-Unsubscribe"], `<https://pouchit.net/api/unsubscribe/one-click?u=${user().id}&t=${token}>`);
    assert.equal(msg.headers!["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click");
    assert.ok(msg.text.includes(`https://pouchit.net/unsubscribe#u=${user().id}&t=${token}`));
    assert.ok(msg.html.includes("Unsubscribe"));
    assert.ok(msg.text.includes(CTX.postalAddress));
  });

  it("refuses header values with line breaks", async () => {
    assert.throws(() => renderSignIn("PouchIt <alerts@pouchit.net>", "a@example.com\r\nBcc: x@example.com", "https://pouchit.net/x"));
    await assert.rejects(() => renderDigest({ user: user({ email: "a@example.com\nBcc: x@example.com" }), follows: [], profit: [ev()] }, CTX));
  });
});

describe("unsubscribe", () => {
  let env: ReturnType<typeof makeEnv>;
  let net: ReturnType<typeof fakeFetch>;
  beforeEach(() => {
    env = makeEnv();
    net = fakeFetch();
  });
  afterEach(() => net.restore());

  async function userRow(email: string) {
    const [u] = env.DB.rows<{ id: string; unsub_nonce: string }>("SELECT id, unsub_nonce FROM users WHERE email = ?", email);
    return { ...u, token: await unsubscribeToken(env.UNSUBSCRIBE_SECRET, u.id, u.unsub_nonce) };
  }
  const prefs = (id: string) => env.DB.rows("SELECT follow_alerts, profit_alerts FROM preferences WHERE user_id = ?", id)[0];

  it("turns off all alerts immediately, without signing in", async () => {
    await signIn(env, net.mail, "a@example.com");
    const a = await userRow("a@example.com");
    const res = await call(unsubscribePage, env, "/api/unsubscribe", { body: { u: a.id, t: a.token } });
    assert.equal(res.status, 200);
    assert.deepEqual({ ...prefs(a.id) }, { follow_alerts: 0, profit_alerts: 0 });
  });

  it("works as an RFC 8058 one-click POST with no Origin", async () => {
    await signIn(env, net.mail, "a@example.com");
    const a = await userRow("a@example.com");
    const res = await call(oneClick, env, "/api/unsubscribe/one-click", {
      origin: null, contentType: "application/x-www-form-urlencoded", body: "List-Unsubscribe=One-Click", query: `?u=${a.id}&t=${a.token}`,
    });
    assert.equal(res.status, 200);
    assert.deepEqual({ ...prefs(a.id) }, { follow_alerts: 0, profit_alerts: 0 });
  });

  it("can't be forged or used on another user", async () => {
    await signIn(env, net.mail, "a@example.com");
    await signIn(env, net.mail, "b@example.com");
    const a = await userRow("a@example.com");
    const b = await userRow("b@example.com");
    for (const [u, t] of [[b.id, a.token], [a.id, b.token], [b.id, "A".repeat(43)], [b.id, a.token.slice(0, -1) + (a.token.endsWith("A") ? "B" : "A")]]) {
      const res = await call(unsubscribePage, env, "/api/unsubscribe", { body: { u, t } });
      assert.equal(res.status, 400);
    }
    assert.deepEqual({ ...prefs(a.id) }, { follow_alerts: 1, profit_alerts: 1 });
    assert.deepEqual({ ...prefs(b.id) }, { follow_alerts: 1, profit_alerts: 1 });
  });

  it("a token from a different secret doesn't work", async () => {
    await signIn(env, net.mail, "a@example.com");
    const a = await userRow("a@example.com");
    const forged = await unsubscribeToken("attacker-guess-".repeat(4), a.id, a.unsub_nonce);
    const res = await call(unsubscribePage, env, "/api/unsubscribe", { body: { u: a.id, t: forged } });
    assert.equal(res.status, 400);
  });
});
