// Follow from an event card and the welcome email (Login & Email Experience spec, PR 3).

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { afterEach, beforeEach, describe, it } from "node:test";
import * as codeRoute from "../../functions/api/auth/code.ts";
import * as requestRoute from "../../functions/api/auth/request.ts";
import * as resendRoute from "../../functions/api/auth/resend.ts";
import * as verifyRoute from "../../functions/api/auth/verify.ts";
import { onRequestGet as me } from "../../functions/api/me.ts";
import { renderWelcome } from "../../src/lib/email.ts";
import { COOKIE } from "../../src/lib/session.ts";
import { PENDING_COOKIE, RESEND_WAIT } from "../../src/lib/signin.ts";
import { FOLLOWS_MAX } from "../../src/lib/validate.ts";
import { call, codeFromMail, fakeFetch, freshIp, makeEnv, tokenFromMail } from "./helpers.ts";

const POSTAL = "PO Box 123, Springfield, IL 62701";
let env: ReturnType<typeof makeEnv>;
let net: ReturnType<typeof fakeFetch>;
const realNow = Date.now;

beforeEach(() => {
  env = makeEnv();
  env.POSTAL_ADDRESS = POSTAL;
  net = fakeFetch();
});
afterEach(() => {
  net.restore();
  Date.now = realNow;
});

function pendingFrom(res: Response): string {
  return res.headers.getSetCookie().find((c) => c.startsWith(`${PENDING_COOKIE}=`))!.split(";")[0];
}
function sessionFrom(res: Response): string {
  return res.headers.getSetCookie().find((c) => c.startsWith(`${COOKIE}=`))!.split(";")[0];
}
async function request(email: string, follow?: unknown) {
  return call(requestRoute.onRequestPost, env, "/api/auth/request", { body: { email, turnstileToken: "good", follow }, ip: freshIp() });
}
async function signInByCode(email: string, follow?: unknown): Promise<string> {
  const res = await request(email, follow);
  const out = await call(codeRoute.onRequestPost, env, "/api/auth/code", { body: { code: codeFromMail(net.mail) }, cookie: pendingFrom(res) });
  assert.equal(out.status, 200);
  return sessionFrom(out);
}
async function follows(session: string): Promise<string[]> {
  return (await (await call(me, env, "/api/me", { method: "GET", cookie: session })).json()).follows.map((f: { artist: string }) => f.artist);
}
const welcomes = () => net.mail.filter((m) => /You're set/.test(m.body.subject ?? ""));

describe("Alert me while signed out", () => {
  it("follows the artist once the code signs them in", async () => {
    const session = await signInByCode("fan@example.com", "Carly Rae Jepsen");
    assert.deepEqual(await follows(session), ["Carly Rae Jepsen"]);
  });

  it("follows the artist once the link signs them in", async () => {
    await request("fan@example.com", "Sting");
    const res = await call(verifyRoute.onRequestPost, env, "/api/auth/verify", { body: { token: tokenFromMail(net.mail), confirm: true } });
    assert.deepEqual(await follows(sessionFrom(res)), ["Sting"]);
  });

  it("keeps the artist across a resend", async () => {
    const res = await request("fan@example.com", "La Roux");
    const pending = pendingFrom(res);
    const t0 = realNow();
    Date.now = () => t0 + (RESEND_WAIT + 1) * 1000;
    await call(resendRoute.onRequestPost, env, "/api/auth/resend", { body: {}, cookie: pending });
    const out = await call(codeRoute.onRequestPost, env, "/api/auth/code", { body: { code: codeFromMail(net.mail) }, cookie: pending });
    assert.deepEqual(await follows(sessionFrom(out)), ["La Roux"]);
  });

  it("validates the artist server-side, like any follow", async () => {
    for (const bad of ["x".repeat(101), "‮abc", "\u0000", 42, ["Sting"]]) {
      const res = await request("fan@example.com", bad);
      assert.equal(res.status, 400, JSON.stringify(bad));
    }
    assert.equal(env.DB.rows("SELECT * FROM login_tokens").length, 0);
    // Line breaks collapse to a space, as in any follow, so a name can never break an email header.
    await request("fan@example.com", "Evil\r\nBcc: a@b.c");
    assert.equal(env.DB.rows<{ pending_follow: string }>("SELECT pending_follow FROM login_tokens")[0].pending_follow, "Evil Bcc: a@b.c");
  });

  it("doesn't follow past the follow limit, and following twice is a no-op", async () => {
    const session = await signInByCode("fan@example.com", "Sting");
    const user = env.DB.rows<{ id: string }>("SELECT id FROM users")[0].id;
    for (let i = 1; i < FOLLOWS_MAX; i++) {
      env.DB.sqlite.prepare("INSERT INTO follows (user_id, artist, artist_key, created_at) VALUES (?, ?, ?, 0)").run(user, `A${i}`, `a${i}`);
    }
    await signInByCode("fan@example.com", "One Too Many");
    await signInByCode("fan@example.com", "Sting");
    const names = await follows(session).catch(() => []);
    assert.ok(!names.includes("One Too Many"));
    assert.equal(env.DB.rows("SELECT * FROM follows").length, FOLLOWS_MAX);
  });
});

describe("welcome email", () => {
  it("goes out once, after the first sign-in, from the alerts sender, with unsubscribe headers", async () => {
    await signInByCode("fan@example.com", "Carly Rae Jepsen");
    await signInByCode("fan@example.com");
    const w = welcomes();
    assert.equal(w.length, 1);
    const m = w[0].body;
    assert.equal(m.from, "PouchIt <alerts@pouchit.net>");
    assert.equal(m.subject, "You're set: alerts for Carly Rae Jepsen");
    assert.match(m.text, /You follow: Carly Rae Jepsen\./);
    assert.match(m.text, /30% or more/);
    assert.match(m.text, /at most one email after each data refresh/);
    assert.match(m.text, /Manage my alerts: https:\/\/pouchit\.net\/alerts/);
    assert.ok(m.text.includes(POSTAL) && m.html.includes(POSTAL));
    assert.match(m.headers["List-Unsubscribe"], /^<https:\/\/pouchit\.net\/api\/unsubscribe\/one-click\?u=/);
    assert.equal(m.headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click");
    assert.ok(m.html.length > 0 && m.text.length > 0);
    assert.notEqual(env.DB.rows<{ welcomed_at: number | null }>("SELECT welcomed_at FROM users")[0].welcomed_at, null);
  });

  it("isn't sent, or marked sent, while POSTAL_ADDRESS is blank", async () => {
    env.POSTAL_ADDRESS = "  ";
    await signInByCode("fan@example.com");
    assert.equal(welcomes().length, 0);
    assert.equal(env.DB.rows<{ welcomed_at: number | null }>("SELECT welcomed_at FROM users")[0].welcomed_at, null);
  });

  it("escapes the artist name in the HTML", async () => {
    const m = await renderWelcome(
      { id: "u".repeat(22), email: "fan@example.com", unsub_nonce: "n".repeat(22) },
      { follows: ['<img src=x onerror="alert(1)">'], followAlerts: true, profitAlerts: true, threshold: 30 },
      { origin: "https://pouchit.net", from: "PouchIt <alerts@pouchit.net>", unsubscribeSecret: "s".repeat(48), postalAddress: POSTAL },
    );
    assert.doesNotMatch(m.html, /<img/);
    assert.match(m.html, /&lt;img/);
  });

  it("is skipped for accounts that existed before it did", () => {
    const db = new DatabaseSync(":memory:");
    const dir = new URL("../../migrations/", import.meta.url);
    const files = readdirSync(dir).filter((f) => f.endsWith(".sql")).sort();
    for (const f of files.filter((f) => f < "0009")) db.exec(readFileSync(new URL(f, dir), "utf8"));
    db.prepare("INSERT INTO users (id, email, unsub_nonce, created_at) VALUES ('u1', 'old@example.com', 'n', 1700000000)").run();
    db.exec(readFileSync(new URL("0009_follow_and_welcome.sql", dir), "utf8"));
    assert.equal((db.prepare("SELECT welcomed_at FROM users").get() as { welcomed_at: number }).welcomed_at, 1700000000);
  });
});

describe("page wiring", () => {
  it("event cards have an Alert me button that follows, or signs in with the artist", () => {
    const page = readFileSync(new URL("../../templates/index.html", import.meta.url), "utf8");
    assert.match(page, /el\("button", "alert-me", "Alert me"\)/);
    assert.match(page, /"\/alerts\?follow=" \+ encodeURIComponent\(name\)/);
    assert.match(page, /fetch\("\/api\/follows"/);
  });

  it("the alerts page sends the artist with the sign-in request and shows it with textContent", () => {
    const js = readFileSync(new URL("../../templates/static/assets/alerts.js", import.meta.url), "utf8");
    assert.match(js, /"You'll get alerts for " \+ follow/);
    assert.match(js, /\$\("follow-note"\)\.textContent =/);
    assert.match(js, /next: "\/alerts", follow \}/);
  });
});
