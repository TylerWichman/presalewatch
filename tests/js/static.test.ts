// Source-level guards: no string-built SQL, no HTML injection sinks in page scripts, no secrets in config.

import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

function files(dir: string, ext: RegExp): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(ROOT, dir))) {
    const rel = join(dir, name);
    if (statSync(join(ROOT, rel)).isDirectory()) out.push(...files(rel, ext));
    else if (ext.test(name)) out.push(rel);
  }
  return out;
}

describe("source guards", () => {
  it("every D1 query is a fixed string with bound parameters", () => {
    for (const f of [...files("src", /\.ts$/), ...files("functions", /\.ts$/), ...files("worker/src", /\.ts$/)]) {
      const src = readFileSync(join(ROOT, f), "utf8");
      for (const m of src.matchAll(/\.prepare\(\s*(`[^`]*`|"[^"]*"|[^)"`]+)/g)) {
        assert.ok(!m[1].includes("${"), `${f}: template interpolation in SQL: ${m[1]}`);
        assert.ok(/^[`"]/.test(m[1]), `${f}: SQL built from a variable: ${m[1]}`);
      }
      assert.doesNotMatch(src, /\.exec\(/, `${f}: raw exec`);
    }
  });

  it("account page scripts never write HTML strings into the page", () => {
    for (const f of files("templates/static", /\.js$/)) {
      const src = readFileSync(join(ROOT, f), "utf8");
      assert.doesNotMatch(src, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/, f);
    }
  });

  it("wrangler configs hold no secrets", () => {
    for (const f of ["wrangler.toml", "worker/wrangler.toml"]) {
      const src = readFileSync(join(ROOT, f), "utf8");
      assert.doesNotMatch(src, /re_[A-Za-z0-9]{16,}|RESEND_API_KEY\s*=|SECRET\s*=|TOKEN\s*=/, f);
    }
  });

  it("the Worker exposes no fetch handler and no public URL", () => {
    const src = readFileSync(join(ROOT, "worker/src/index.ts"), "utf8");
    assert.doesNotMatch(src, /\bfetch\s*\(\s*request|async fetch\(/);
    const toml = readFileSync(join(ROOT, "worker/wrangler.toml"), "utf8");
    assert.match(toml, /^workers_dev = false$/m);
    assert.match(toml, /^preview_urls = false$/m);
    assert.doesNotMatch(toml, /^\s*routes?\s*=/m);
  });
});
