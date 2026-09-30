import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import { onRequestPost as requestLink } from "../../functions/api/auth/request.ts";
import { onRequestPost as verifyLink } from "../../functions/api/auth/verify.ts";
import { LIMITS } from "../../src/lib/ratelimit.ts";
import { call, fakeFetch, freshIp, makeEnv } from "./helpers.ts";

let env: ReturnType<typeof makeEnv>;
let net: ReturnType<typeof fakeFetch>;
beforeEach(() => {
  env = makeEnv();
  net = fakeFetch();
});
afterEach(() => net.restore());

const req = (email: string, ip: string) => call(requestLink, env, "/api/auth/request", { body: { email, turnstileToken: "good" }, ip });

describe("rate limits", () => {
  it("limits sign-in requests per IP with a 429", async () => {
    const max = LIMITS.signinIp[0].max;
    for (let i = 0; i < max; i++) assert.equal((await req(`u${i}@example.com`, "192.0.2.1")).status, 200);
    const res = await req("another@example.com", "192.0.2.1");
    assert.equal(res.status, 429);
    assert.equal(net.mail.length, max);
    assert.equal((await req("another@example.com", "192.0.2.2")).status, 200);
  });

  it("limits emails per address silently, with the normal response", async () => {
    const max = LIMITS.signinEmail[0].max;
    const answers = [];
    for (let i = 0; i < max + 2; i++) answers.push(await (await req("target@example.com", freshIp())).json());
    assert.equal(net.mail.length, max, "no more emails than the per-address limit");
    for (const a of answers) assert.deepEqual(a, answers[0], "responses don't reveal the limit");
  });

  it("caps sign-in emails globally to protect the sending quota", async () => {
    const max = LIMITS.signinGlobal[0].max;
    for (let i = 0; i < max + 5; i++) await req(`g${i}@example.com`, `10.0.${Math.floor(i / 4)}.${i % 4}`);
    assert.equal(net.mail.length, max);
  });

  it("limits verify attempts per IP", async () => {
    const max = LIMITS.verifyIp[0].max;
    for (let i = 0; i < max; i++) {
      assert.equal((await call(verifyLink, env, "/api/auth/verify", { body: { token: "A".repeat(43) }, ip: "192.0.2.9" })).status, 400);
    }
    assert.equal((await call(verifyLink, env, "/api/auth/verify", { body: { token: "A".repeat(43) }, ip: "192.0.2.9" })).status, 429);
  });

  it("stores no raw IPs or emails in the rate limit table", async () => {
    await req("private@example.com", "192.0.2.77");
    const dump = JSON.stringify(env.DB.rows("SELECT * FROM rate_limits"));
    assert.ok(!dump.includes("192.0.2.77"));
    assert.ok(!dump.includes("private@example.com"));
  });
});
