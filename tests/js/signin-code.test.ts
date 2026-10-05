// Sign-in by 6-digit code (Login & Email Experience spec, PR 1): one test per security rule.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, it } from "node:test";
import * as codeRoute from "../../functions/api/auth/code.ts";
import * as requestRoute from "../../functions/api/auth/request.ts";
import * as resendRoute from "../../functions/api/auth/resend.ts";
import * as verifyRoute from "../../functions/api/auth/verify.ts";
import { onRequestGet as me } from "../../functions/api/me.ts";
import { sha256 } from "../../src/lib/crypto.ts";
import { COOKIE } from "../../src/lib/session.ts";
import { CODE_ATTEMPTS, PENDING_COOKIE, randomCode, RESEND_WAIT, TOKEN_TTL } from "../../src/lib/signin.ts";
import { call, codeFromMail, fakeFetch, freshIp, makeEnv, tokenFromMail } from "./helpers.ts";

let env: ReturnType<typeof makeEnv>;
let net: ReturnType<typeof fakeFetch>;
const realNow = Date.now;

beforeEach(() => {
  env = makeEnv();
  net = fakeFetch();
});
afterEach(() => {
  net.restore();
  Date.now = realNow;
});

function cookies(res: Response): string[] {
  return res.headers.getSetCookie();
}
function pendingFrom(res: Response): string {
  const c = cookies(res).find((v) => v.startsWith(`${PENDING_COOKIE}=`));
  if (!c) throw new Error("no pending cookie");
  return c.split(";")[0];
}
function sessionFrom(res: Response): string | undefined {
  return cookies(res).find((v) => v.startsWith(`${COOKIE}=`))?.split(";")[0];
}
async function request(email: string, ip = freshIp()) {
  const res = await call(requestRoute.onRequestPost, env, "/api/auth/request", { body: { email, turnstileToken: "good" }, ip });
  return { res, pending: pendingFrom(res) };
}
function sendCode(code: string, pending?: string, ip = freshIp(), next?: string) {
  return call(codeRoute.onRequestPost, env, "/api/auth/code", { body: { code, next }, cookie: pending, ip });
}
function wrong(code: string): string {
  return String((Number(code) + 1) % 1_000_000).padStart(6, "0");
}

describe("sign-in code", () => {
  it("is 6 digits from a secure random source, emailed, and stored only as a keyed hash", async () => {
    const seen = new Set<string>();
    for (let i = 0; i < 2000; i++) {
      const c = randomCode();
      assert.match(c, /^[0-9]{6}$/);
      seen.add(c);
    }
    assert.ok(seen.size > 1990, "codes don't repeat in practice");
    await request("a@example.com");
    const code = codeFromMail(net.mail);
    const row = env.DB.rows<{ code_hash: string; token_hash: string }>("SELECT code_hash, token_hash FROM login_tokens")[0];
    const stored = JSON.stringify(env.DB.rows("SELECT * FROM login_tokens"));
    assert.ok(!stored.includes(code), "the code itself is never stored");
    assert.notEqual(row.code_hash, await sha256(code), "not a plain (unkeyed) hash either");
  });

  it("signs in the browser that asked for it, e.g. code read on a phone, typed on a laptop", async () => {
    const { pending } = await request("a@example.com");
    const res = await sendCode(codeFromMail(net.mail), pending);
    assert.equal(res.status, 200);
    const session = sessionFrom(res)!;
    assert.ok(session);
    assert.ok(cookies(res).some((c) => c.startsWith(`${PENDING_COOKIE}=;`) && c.includes("Max-Age=0")), "pending cookie cleared");
    assert.equal((await (await call(me, env, "/api/me", { method: "GET", cookie: session })).json()).signedIn, true);
  });

  it("only works in the browser that requested it", async () => {
    await request("a@example.com");
    const code = codeFromMail(net.mail);
    const none = await sendCode(code);
    assert.equal(none.status, 400);
    const { pending: attacker } = await request("attacker@example.com");
    const other = await sendCode(code, attacker);
    assert.equal(other.status, 400);
    assert.equal(sessionFrom(other), undefined);
  });

  it("is dead after 5 wrong tries, even for the right code, and says how many are left", async () => {
    const { pending } = await request("a@example.com");
    const code = codeFromMail(net.mail);
    for (let i = 1; i <= CODE_ATTEMPTS; i++) {
      const res = await sendCode(wrong(code), pending);
      const body = await res.json();
      assert.equal(res.status, 400);
      assert.equal(body.attemptsLeft, CODE_ATTEMPTS - i);
      if (i < CODE_ATTEMPTS) assert.match(body.error, new RegExp(`${CODE_ATTEMPTS - i} tr(y|ies) left`));
      else assert.equal(body.restart, true);
    }
    const right = await sendCode(code, pending);
    assert.equal(right.status, 400);
    assert.equal((await right.json()).restart, true);
    assert.equal(sessionFrom(right), undefined);
  });

  it("checks at most 5 guesses per request even when they arrive at once", async () => {
    const { pending } = await request("a@example.com");
    const code = codeFromMail(net.mail);
    const guesses = Array.from({ length: 12 }, (_, i) => String((Number(code) + 1 + i) % 1_000_000).padStart(6, "0"));
    await Promise.all(guesses.map((g) => sendCode(g, pending)));
    const attempts = env.DB.rows<{ attempts: number }>("SELECT attempts FROM login_tokens")[0].attempts;
    assert.equal(attempts, CODE_ATTEMPTS);
  });

  it("expires after 15 minutes", async () => {
    const { pending } = await request("a@example.com");
    const code = codeFromMail(net.mail);
    const t0 = realNow();
    Date.now = () => t0 + (TOKEN_TTL + 5) * 1000;
    const res = await sendCode(code, pending);
    assert.equal(res.status, 400);
    assert.equal((await res.json()).restart, true);
  });

  it("using the code ends the link, and using the link ends the code", async () => {
    const first = await request("a@example.com");
    const token = tokenFromMail(net.mail);
    assert.equal((await sendCode(codeFromMail(net.mail), first.pending)).status, 200);
    const link = await call(verifyRoute.onRequestPost, env, "/api/auth/verify", { body: { token, confirm: true } });
    assert.equal(link.status, 400);

    const second = await request("b@example.com");
    const code = codeFromMail(net.mail);
    const ok = await call(verifyRoute.onRequestPost, env, "/api/auth/verify", { body: { token: tokenFromMail(net.mail), confirm: true } });
    assert.equal(ok.status, 200);
    assert.equal((await sendCode(code, second.pending)).status, 400);
  });

  it("rate-limits code tries per IP", async () => {
    const { pending } = await request("a@example.com");
    const ip = "203.0.113.99";
    const statuses: number[] = [];
    for (let i = 0; i < 22; i++) {
      // A fresh request each round so the per-request limit isn't what stops it.
      const r = await request("a" + i + "@example.com");
      statuses.push((await sendCode("000000", r.pending, ip)).status);
    }
    assert.ok(statuses.includes(429));
    assert.ok(pending);
  });

  it("caps code tries at 50 a day per address, across requests, browsers, and IPs", async () => {
    const t0 = Date.UTC(2026, 9, 6, 1, 0, 0); // well inside one UTC day
    let pending = "";
    const statuses: number[] = [];
    for (let i = 0; i < 60; i++) {
      // 70 seconds apart: under the 15-per-15-minutes limit, so only the daily cap can stop it.
      Date.now = () => t0 + i * 70 * 1000;
      if (i % CODE_ATTEMPTS === 0) pending = (await request("target@example.com")).pending;
      statuses.push((await sendCode("000000", pending)).status);
    }
    assert.ok(statuses.slice(0, 50).every((s) => s === 400), "the first 50 are just wrong");
    assert.ok(statuses.slice(50).every((s) => s === 429), "from the 51st on, refused");
  });

  it("sends a session only to allowlisted redirects", async () => {
    const { pending } = await request("a@example.com");
    const res = await sendCode(codeFromMail(net.mail), pending, freshIp(), "https://evil.example/");
    assert.equal((await res.json()).next, "/alerts");
  });

  it("rotates the session ID on sign-in", async () => {
    const one = await request("a@example.com");
    const first = sessionFrom(await sendCode(codeFromMail(net.mail), one.pending))!;
    const two = await request("a@example.com");
    const res = await call(codeRoute.onRequestPost, env, "/api/auth/code", {
      body: { code: codeFromMail(net.mail) },
      cookie: `${first}; ${two.pending}`,
    });
    const second = sessionFrom(res)!;
    assert.notEqual(first, second);
    assert.equal((await (await call(me, env, "/api/me", { method: "GET", cookie: first })).json()).signedIn, false);
  });

  it("needs the same Origin and JSON checks as every other state change", async () => {
    const { pending } = await request("a@example.com");
    const code = codeFromMail(net.mail);
    const cross = await call(codeRoute.onRequestPost, env, "/api/auth/code", { body: { code }, cookie: pending, origin: "https://evil.example" });
    assert.equal(cross.status, 403);
    const form = await call(codeRoute.onRequestPost, env, "/api/auth/code", { body: `code=${code}`, cookie: pending, contentType: "application/x-www-form-urlencoded" });
    assert.equal(form.status, 415);
  });
});

describe("pending-login cookie", () => {
  it("is host-only, HttpOnly, Secure, SameSite=Strict, and lasts 15 minutes", async () => {
    const { res } = await request("a@example.com");
    const c = cookies(res).find((v) => v.startsWith(`${PENDING_COOKIE}=`))!;
    assert.ok(PENDING_COOKIE.startsWith("__Host-"));
    for (const flag of ["Path=/", "HttpOnly", "Secure", "SameSite=Strict", `Max-Age=${TOKEN_TTL}`]) assert.ok(c.includes(flag), flag);
    assert.ok(!/Domain=/i.test(c));
  });
});

describe("no account enumeration", () => {
  it("request and resend answer the same whether or not the address has an account", async () => {
    const existing = await request("exists@example.com");
    assert.equal((await sendCode(codeFromMail(net.mail), existing.pending)).status, 200);
    const t0 = realNow();
    const a = await request("exists@example.com");
    const b = await request("nobody@example.com");
    assert.deepEqual([a.res.status, await a.res.json()], [b.res.status, await b.res.json()]);
    const strip = (s: string) => s.replace(/=[^;]*/, "=X");
    assert.equal(strip(cookies(a.res)[0]), strip(cookies(b.res)[0]));
    Date.now = () => t0 + (RESEND_WAIT + 1) * 1000;
    const ra = await call(resendRoute.onRequestPost, env, "/api/auth/resend", { body: {}, cookie: a.pending, ip: freshIp() });
    const rb = await call(resendRoute.onRequestPost, env, "/api/auth/resend", { body: {}, cookie: b.pending, ip: freshIp() });
    assert.deepEqual([ra.status, await ra.json()], [rb.status, await rb.json()]);
  });
});

describe("resend", () => {
  it("waits 30 seconds, then sends a new code and link and ends the old ones", async () => {
    const { pending } = await request("a@example.com");
    const oldCode = codeFromMail(net.mail);
    const oldToken = tokenFromMail(net.mail);
    const early = await call(resendRoute.onRequestPost, env, "/api/auth/resend", { body: {}, cookie: pending });
    assert.equal(early.status, 429);
    const t0 = realNow();
    Date.now = () => t0 + (RESEND_WAIT + 1) * 1000;
    const res = await call(resendRoute.onRequestPost, env, "/api/auth/resend", { body: {}, cookie: pending });
    assert.equal(res.status, 200);
    const newCode = codeFromMail(net.mail);
    assert.notEqual(tokenFromMail(net.mail), oldToken);
    const stale = await call(verifyRoute.onRequestPost, env, "/api/auth/verify", { body: { token: oldToken, confirm: true } });
    assert.equal(stale.status, 400);
    if (newCode !== oldCode) assert.equal((await sendCode(oldCode, pending)).status, 400);
    assert.equal((await sendCode(newCode, pending)).status, 200);
  });

  it("still obeys the per-address sign-in limit", async () => {
    const { pending } = await request("a@example.com");
    const t0 = realNow();
    const sent = () => net.mail.filter((m) => m.url.endsWith("/emails")).length;
    for (let i = 1; i <= 12; i++) {
      Date.now = () => t0 + i * (RESEND_WAIT + 1) * 1000;
      await call(resendRoute.onRequestPost, env, "/api/auth/resend", { body: {}, cookie: pending, ip: freshIp() });
    }
    assert.ok(sent() <= 3, `sent ${sent()} emails in 15 minutes`);
  });
});

describe("sign-in link", () => {
  it("a plain GET of the link changes nothing: the token stays usable", async () => {
    await request("a@example.com");
    const token = tokenFromMail(net.mail);
    const link = /https:\/\/[^\s]+/.exec(net.mail.at(-1)!.body.text)![0];
    // The token is in the fragment, which a GET (by a person or a mail scanner) never sends.
    const url = new URL(link);
    assert.equal(url.pathname, "/auth/confirm");
    assert.equal(url.search, "");
    assert.ok(url.hash.includes(token));
    // The verify route has no GET handler, and the confirm page consumes only on the button press.
    assert.equal("onRequestGet" in verifyRoute, false);
    const confirmJs = readFileSync(new URL("../../templates/static/assets/confirm.js", import.meta.url), "utf8");
    const beforeClick = confirmJs.slice(0, confirmJs.indexOf('addEventListener("click"'));
    assert.doesNotMatch(beforeClick, /verify\(true\)/);
    // Opening the page runs the preview, which doesn't use the token up.
    await call(verifyRoute.onRequestPost, env, "/api/auth/verify", { body: { token } });
    assert.equal(env.DB.rows("SELECT used_at FROM login_tokens WHERE used_at IS NULL").length, 1);
    const res = await call(verifyRoute.onRequestPost, env, "/api/auth/verify", { body: { token, confirm: true } });
    assert.equal(res.status, 200);
  });
});
