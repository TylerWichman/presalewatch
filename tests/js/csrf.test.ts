import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import { onRequestPost as logout } from "../../functions/api/auth/logout.ts";
import { onRequestPost as requestLink } from "../../functions/api/auth/request.ts";
import { onRequestPost as verifyLink } from "../../functions/api/auth/verify.ts";
import { onRequestDelete as unfollow } from "../../functions/api/follows/[id].ts";
import { onRequestPost as follow } from "../../functions/api/follows/index.ts";
import { onRequestDelete as deleteMe } from "../../functions/api/me.ts";
import { onRequestPut as putPrefs } from "../../functions/api/preferences.ts";
import { onRequestPost as unsubscribe } from "../../functions/api/unsubscribe/index.ts";
import { onRequestPost as oneClick } from "../../functions/api/unsubscribe/one-click.ts";
import { onRequest as middleware } from "../../functions/api/_middleware.ts";
import type { Handler } from "../../src/lib/http.ts";
import { call, fakeFetch, makeEnv, signIn } from "./helpers.ts";

let env: ReturnType<typeof makeEnv>;
let net: ReturnType<typeof fakeFetch>;
beforeEach(() => {
  env = makeEnv();
  net = fakeFetch();
});
afterEach(() => net.restore());

const ENDPOINTS: [string, Handler, string, string, unknown][] = [
  ["request link", requestLink, "POST", "/api/auth/request", { email: "a@example.com", turnstileToken: "good" }],
  ["verify link", verifyLink, "POST", "/api/auth/verify", { token: "A".repeat(43), confirm: true }],
  ["logout", logout, "POST", "/api/auth/logout", {}],
  ["preferences", putPrefs, "PUT", "/api/preferences", { followAlerts: false, profitAlerts: false, profitThreshold: 10 }],
  ["follow", follow, "POST", "/api/follows", { artist: "Phoebe Bridgers" }],
  ["unfollow", unfollow, "DELETE", "/api/follows/1", undefined],
  ["delete account", deleteMe, "DELETE", "/api/me", undefined],
  ["unsubscribe", unsubscribe, "POST", "/api/unsubscribe", { u: "x", t: "y" }],
];

describe("CSRF", () => {
  for (const [name, handler, method, path, body] of ENDPOINTS) {
    it(`${name}: rejects a missing or foreign Origin`, async () => {
      const cookie = await signIn(env, net.mail, "victim@example.com");
      const before = JSON.stringify(env.DB.rows("SELECT * FROM preferences")) + JSON.stringify(env.DB.rows("SELECT * FROM users"));
      for (const origin of [null, "https://evil.example", "https://presalewatch.pages.dev", "http://pouchit.net", "https://pouchit.net.evil.example", "null"]) {
        const res = await call(handler, env, path, { method, body, cookie, origin, params: { id: "1" } });
        assert.equal(res.status, 403, `${name} with Origin ${origin}`);
      }
      const after = JSON.stringify(env.DB.rows("SELECT * FROM preferences")) + JSON.stringify(env.DB.rows("SELECT * FROM users"));
      assert.equal(after, before);
    });
  }

  for (const [name, handler, method, path, body] of ENDPOINTS.filter((e) => e[2] !== "DELETE")) {
    it(`${name}: rejects simple-request content types that skip CORS preflight`, async () => {
      for (const contentType of ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", null]) {
        const res = await call(handler, env, path, { method, body, contentType });
        assert.equal(res.status, 415, `${name} with ${contentType}`);
      }
    });
  }

  it("one-click unsubscribe skips the Origin check but still needs a valid signed token", async () => {
    const res = await call(oneClick, env, "/api/unsubscribe/one-click", {
      origin: null, contentType: "application/x-www-form-urlencoded", body: "List-Unsubscribe=One-Click",
      query: `?u=${"A".repeat(22)}&t=${"B".repeat(43)}`,
    });
    assert.equal(res.status, 400);
  });

  it("the middleware blocks bad requests before any route runs and adds security headers", async () => {
    let ran = false;
    const next = async () => {
      ran = true;
      return new Response("ok");
    };
    const bad = new Request("https://pouchit.net/api/follows", { method: "POST", headers: { Origin: "https://evil.example", "Content-Type": "application/json" }, body: "{}" });
    const res = await middleware({ request: bad, env, next });
    assert.equal(res.status, 403);
    assert.equal(ran, false);
    const good = new Request("https://pouchit.net/api/me", { method: "GET" });
    const ok = await middleware({ request: good, env, next });
    assert.equal(ok.headers.get("X-Frame-Options"), "DENY");
    assert.match(ok.headers.get("Content-Security-Policy")!, /frame-ancestors 'none'/);
    assert.match(ok.headers.get("Strict-Transport-Security")!, /max-age=\d+/);
    assert.equal(ok.headers.get("X-Content-Type-Options"), "nosniff");
    assert.equal(ok.headers.get("Referrer-Policy"), "no-referrer");
    assert.ok(ok.headers.get("Permissions-Policy"));
    assert.equal(ok.headers.get("Cache-Control"), "no-store");
  });

  it("errors return a generic message without internals", async () => {
    const next = async (): Promise<Response> => {
      throw new Error("D1_ERROR: no such table: users at SELECT email FROM users WHERE id = 'abc'");
    };
    const res = await middleware({ request: new Request("https://pouchit.net/api/me"), env, next });
    assert.equal(res.status, 500);
    const body = await res.text();
    assert.doesNotMatch(body, /D1|SELECT|users/);
  });
});
