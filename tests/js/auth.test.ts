import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import { onRequestPost as logout } from "../../functions/api/auth/logout.ts";
import { onRequestPost as requestLink, SIGNIN_MESSAGE, TOKEN_TTL } from "../../functions/api/auth/request.ts";
import { onRequestPost as verifyLink } from "../../functions/api/auth/verify.ts";
import { onRequestGet as me } from "../../functions/api/me.ts";
import { sha256 } from "../../src/lib/crypto.ts";
import { ABSOLUTE_TTL, COOKIE, IDLE_TTL } from "../../src/lib/session.ts";
import { call, cookieFrom, fakeFetch, freshIp, makeEnv, signIn, tokenFromMail } from "./helpers.ts";

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

async function requestFor(email: string, ip = freshIp()) {
  return call(requestLink, env, "/api/auth/request", { body: { email, turnstileToken: "good" }, ip });
}

describe("magic link request", () => {
  it("answers identically whether or not the account exists", async () => {
    await signIn(env, net.mail, "exists@example.com");
    const a = await requestFor("exists@example.com");
    const b = await requestFor("nobody@example.com");
    assert.equal(a.status, 200);
    assert.equal(b.status, 200);
    assert.deepEqual(await a.json(), await b.json());
    assert.deepEqual(await (await requestFor("exists@example.com")).json(), { ok: true, message: SIGNIN_MESSAGE });
  });

  it("stores only a hash of a 256-bit token that expires in 15 minutes", async () => {
    await requestFor("a@example.com");
    const token = tokenFromMail(net.mail);
    assert.equal(token.length, 43); // 32 random bytes
    const rows = env.DB.rows<{ token_hash: string; expires_at: number }>("SELECT token_hash, expires_at FROM login_tokens");
    assert.equal(rows.length, 1);
    assert.notEqual(rows[0].token_hash, token);
    assert.equal(rows[0].token_hash, await sha256(token));
    assert.ok(Math.abs(rows[0].expires_at - (Math.floor(Date.now() / 1000) + TOKEN_TTL)) <= 2);
    assert.ok(!JSON.stringify(env.DB.rows("SELECT * FROM login_tokens")).includes(token));
  });

  it("rejects a missing or failed Turnstile check and sends nothing", async () => {
    const res = await call(requestLink, env, "/api/auth/request", { body: { email: "a@example.com", turnstileToken: "bad" } });
    assert.equal(res.status, 400);
    const res2 = await call(requestLink, env, "/api/auth/request", { body: { email: "a@example.com" }, ip: freshIp() });
    assert.equal(res2.status, 400);
    assert.equal(net.mail.length, 0);
  });

  it("rejects invalid addresses, including header injection attempts", async () => {
    for (const bad of ["not-an-email", "a@b", "a@example.com\r\nBcc: victim@example.com", "a@example.com,b@example.com", "<a@example.com>", "a".repeat(250) + "@x.com"]) {
      const res = await requestFor(bad);
      assert.equal(res.status, 400, bad);
    }
    assert.equal(net.mail.length, 0);
  });

  it("puts the token in the URL fragment, never the query string", async () => {
    await requestFor("a@example.com");
    const text: string = net.mail[0].body.text;
    assert.match(text, /https:\/\/pouchit\.net\/auth\/confirm#token=/);
    assert.doesNotMatch(text, /\?token=/);
  });

  it("only redirects to allowlisted same-site paths", async () => {
    let n = 0;
    for (const [next, expected] of [["/", "/"], ["/alerts", "/alerts"], ["https://evil.example", "/alerts"], ["//evil.example", "/alerts"], ["/\\evil.example", "/alerts"], ["javascript:alert(1)", "/alerts"]]) {
      const ip = freshIp();
      n += 1;
      await call(requestLink, env, "/api/auth/request", { body: { email: `r${n}@example.com`, turnstileToken: "good", next }, ip });
      const token = tokenFromMail(net.mail);
      const res = await call(verifyLink, env, "/api/auth/verify", { body: { token, confirm: true, next }, ip });
      assert.equal((await res.json()).next, expected, String(next));
      assert.ok(net.mail.at(-1)!.body.text.includes(`next=${encodeURIComponent(expected)}`));
    }
  });
});

describe("magic link verify", () => {
  it("previews the masked address without using up the token", async () => {
    await requestFor("person@example.com");
    const token = tokenFromMail(net.mail);
    const peek = await call(verifyLink, env, "/api/auth/verify", { body: { token } });
    assert.deepEqual(await peek.json(), { ok: true, email: "p•••@example.com" });
    const res = await call(verifyLink, env, "/api/auth/verify", { body: { token, confirm: true } });
    assert.equal(res.status, 200);
  });

  it("is single use", async () => {
    await requestFor("a@example.com");
    const token = tokenFromMail(net.mail);
    const first = await call(verifyLink, env, "/api/auth/verify", { body: { token, confirm: true } });
    assert.equal(first.status, 200);
    const again = await call(verifyLink, env, "/api/auth/verify", { body: { token, confirm: true } });
    assert.equal(again.status, 400);
    assert.equal(again.headers.get("Set-Cookie"), null);
  });

  it("rejects expired tokens", async () => {
    await requestFor("a@example.com");
    const token = tokenFromMail(net.mail);
    const t0 = realNow();
    Date.now = () => t0 + (TOKEN_TTL + 1) * 1000;
    const res = await call(verifyLink, env, "/api/auth/verify", { body: { token, confirm: true } });
    assert.equal(res.status, 400);
  });

  it("invalidates the user's other outstanding links after one is used", async () => {
    await requestFor("a@example.com");
    const older = tokenFromMail(net.mail);
    await requestFor("a@example.com");
    const newer = tokenFromMail(net.mail);
    assert.equal((await call(verifyLink, env, "/api/auth/verify", { body: { token: newer, confirm: true } })).status, 200);
    assert.equal((await call(verifyLink, env, "/api/auth/verify", { body: { token: older, confirm: true } })).status, 400);
  });

  it("rejects malformed and unknown tokens with the same message", async () => {
    const a = await call(verifyLink, env, "/api/auth/verify", { body: { token: "x", confirm: true } });
    const b = await call(verifyLink, env, "/api/auth/verify", { body: { token: "A".repeat(43), confirm: true } });
    assert.equal(a.status, 400);
    assert.equal(b.status, 400);
    assert.deepEqual(await a.json(), await b.json());
  });

  it("creates the account only when a link is confirmed", async () => {
    await requestFor("new@example.com");
    assert.equal(env.DB.rows("SELECT * FROM users").length, 0);
    const token = tokenFromMail(net.mail);
    await call(verifyLink, env, "/api/auth/verify", { body: { token, confirm: true } });
    assert.equal(env.DB.rows("SELECT * FROM users").length, 1);
    assert.deepEqual(env.DB.rows("SELECT follow_alerts, profit_alerts, profit_threshold FROM preferences").map((r) => ({ ...r })), [
      { follow_alerts: 1, profit_alerts: 1, profit_threshold: 30 },
    ]);
  });
});

describe("sessions", () => {
  it("sets a hardened cookie and stores only a hash of the session ID", async () => {
    await requestFor("a@example.com");
    const token = tokenFromMail(net.mail);
    const res = await call(verifyLink, env, "/api/auth/verify", { body: { token, confirm: true } });
    const set = res.headers.get("Set-Cookie")!;
    assert.match(set, new RegExp(`^${COOKIE}=[A-Za-z0-9_-]{43}; `));
    for (const flag of ["HttpOnly", "Secure", "SameSite=Lax", "Path=/", `Max-Age=${ABSOLUTE_TTL}`]) assert.ok(set.includes(flag), flag);
    assert.ok(!set.includes("Domain="));
    const id = cookieFrom(res).split("=")[1];
    const rows = env.DB.rows<{ id_hash: string }>("SELECT id_hash FROM sessions");
    assert.deepEqual(rows.map((r) => r.id_hash), [await sha256(id)]);
  });

  it("rotates the session on login", async () => {
    const first = await signIn(env, net.mail, "a@example.com");
    await requestFor("a@example.com");
    const token = tokenFromMail(net.mail);
    const res = await call(verifyLink, env, "/api/auth/verify", { body: { token, confirm: true }, cookie: first });
    const second = cookieFrom(res);
    assert.notEqual(first, second);
    assert.equal(env.DB.rows("SELECT * FROM sessions").length, 1);
    assert.equal((await (await call(me, env, "/api/me", { method: "GET", cookie: first })).json()).signedIn, false);
    assert.equal((await (await call(me, env, "/api/me", { method: "GET", cookie: second })).json()).signedIn, true);
  });

  it("revokes the session on logout", async () => {
    const cookie = await signIn(env, net.mail, "a@example.com");
    const res = await call(logout, env, "/api/auth/logout", { body: {}, cookie });
    assert.match(res.headers.get("Set-Cookie")!, /Max-Age=0/);
    assert.equal(env.DB.rows("SELECT * FROM sessions").length, 0);
    assert.equal((await (await call(me, env, "/api/me", { method: "GET", cookie })).json()).signedIn, false);
  });

  it("expires after the idle timeout", async () => {
    const cookie = await signIn(env, net.mail, "a@example.com");
    const t0 = realNow();
    Date.now = () => t0 + (IDLE_TTL + 60) * 1000;
    assert.equal((await (await call(me, env, "/api/me", { method: "GET", cookie })).json()).signedIn, false);
    assert.equal(env.DB.rows("SELECT * FROM sessions").length, 0);
  });

  it("expires at the absolute limit even when active", async () => {
    const cookie = await signIn(env, net.mail, "a@example.com");
    const t0 = realNow();
    // Stay active every few days until past the absolute limit.
    for (let day = 3; day * 86400 < ABSOLUTE_TTL; day += 3) {
      Date.now = () => t0 + day * 86400 * 1000;
      assert.equal((await (await call(me, env, "/api/me", { method: "GET", cookie })).json()).signedIn, true, `day ${day}`);
    }
    Date.now = () => t0 + (ABSOLUTE_TTL + 60) * 1000;
    assert.equal((await (await call(me, env, "/api/me", { method: "GET", cookie })).json()).signedIn, false);
  });

  it("ignores forged or malformed cookies", async () => {
    await signIn(env, net.mail, "a@example.com");
    for (const cookie of [`${COOKIE}=${"A".repeat(43)}`, `${COOKIE}=' OR 1=1 --`, `${COOKIE}=`]) {
      assert.equal((await (await call(me, env, "/api/me", { method: "GET", cookie })).json()).signedIn, false);
    }
  });
});
