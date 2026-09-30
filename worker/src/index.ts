// PresaleWatch alert sender. Runs hourly on a cron trigger and has no fetch handler, no
// workers.dev URL, and no routes, so nothing on the internet can call it.
//
// Each run: clean up expired rows, fetch alerts.json from the site, and if the data is new since
// the last complete run, email each user one digest of events they haven't been sent before.

import { BATCH_MAX, renderDigest, sendBatch, type Message } from "../../src/lib/email.ts";
import { log } from "../../src/lib/http.ts";
import { buildDigests, parseFeed, type AlertUser, type Digest } from "../../src/lib/match.ts";

export interface WorkerEnv {
  DB: D1Database;
  APP_ORIGIN: string;
  EMAIL_FROM: string;
  RESEND_API_KEY: string;
  UNSUBSCRIBE_SECRET: string;
  POSTAL_ADDRESS: string;
  DRY_RUN: string;
  MAX_EMAILS_PER_RUN: string;
}

const FEED_MAX_BYTES = 5_000_000;
// Dry runs keep their own marker, so switching DRY_RUN off still sends the current data for real.
const STATE_KEY = "last_complete_generated_at";
const DRY_RUN_STATE_KEY = "dry_run_generated_at";

export async function cleanup(env: WorkerEnv, now: number): Promise<void> {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM login_tokens WHERE expires_at <= ?1").bind(now - 3600),
    env.DB.prepare("DELETE FROM sessions WHERE expires_at <= ?1").bind(now),
    env.DB.prepare("DELETE FROM rate_limits WHERE window_start <= ?1").bind(now - 2 * 86400),
  ]);
}

async function fetchFeed(env: WorkerEnv) {
  const res = await fetch(`${env.APP_ORIGIN}/alerts.json`, { headers: { Accept: "application/json" }, cf: { cacheTtl: 0 } } as RequestInit);
  if (!res.ok) throw new Error(`alerts.json returned HTTP ${res.status}`);
  const text = await res.text();
  if (text.length > FEED_MAX_BYTES) throw new Error("alerts.json is too large");
  return parseFeed(JSON.parse(text));
}

async function loadAlertUsers(env: WorkerEnv) {
  const users = await env.DB.prepare(
    `SELECT u.id, u.email, u.unsub_nonce, p.follow_alerts, p.profit_alerts, p.profit_threshold
     FROM users u JOIN preferences p ON p.user_id = u.id
     WHERE p.follow_alerts = 1 OR p.profit_alerts = 1
     ORDER BY u.created_at, u.id`,
  ).all<AlertUser>();
  const follows = await env.DB.prepare("SELECT user_id, artist_key FROM follows").all<{ user_id: string; artist_key: string }>();
  const sent = await env.DB.prepare("SELECT user_id, event_id FROM sent_alerts").all<{ user_id: string; event_id: string }>();
  const group = (rows: { user_id: string }[], value: (r: never) => string) => {
    const m = new Map<string, Set<string>>();
    for (const r of rows) {
      if (!m.has(r.user_id)) m.set(r.user_id, new Set());
      m.get(r.user_id)!.add(value(r as never));
    }
    return m;
  };
  return {
    users: users.results,
    followsByUser: group(follows.results, (r: { artist_key: string }) => r.artist_key),
    sentByUser: group(sent.results, (r: { event_id: string }) => r.event_id),
  };
}

function sentRows(env: WorkerEnv, digests: Digest[], now: number) {
  return digests.flatMap((d) =>
    [...d.follows, ...d.profit].map((e) =>
      env.DB.prepare("INSERT INTO sent_alerts (user_id, event_id, sent_at) VALUES (?1, ?2, ?3) ON CONFLICT DO NOTHING").bind(d.user.id, e.id, now),
    ),
  );
}

function unsentRows(env: WorkerEnv, digests: Digest[], now: number) {
  return digests.flatMap((d) =>
    [...d.follows, ...d.profit].map((e) =>
      env.DB.prepare("DELETE FROM sent_alerts WHERE user_id = ?1 AND event_id = ?2 AND sent_at = ?3").bind(d.user.id, e.id, now),
    ),
  );
}

export interface RunResult {
  status: "no-new-data" | "complete" | "partial" | "blocked";
  digests: number;
  sent: number;
}

export async function run(env: WorkerEnv, nowDate = new Date()): Promise<RunResult> {
  const now = Math.floor(nowDate.getTime() / 1000);
  const dryRun = env.DRY_RUN !== "false";
  const stateKey = dryRun ? DRY_RUN_STATE_KEY : STATE_KEY;
  await cleanup(env, now);

  const feed = await fetchFeed(env);
  const last = await env.DB.prepare("SELECT value FROM worker_state WHERE key = ?1").bind(stateKey).first<{ value: string }>();
  if (last?.value === feed.generatedAt) {
    log("info", { where: "worker", reason: "no new data", generated_at: feed.generatedAt });
    return { status: "no-new-data", digests: 0, sent: 0 };
  }
  if (!dryRun && !env.POSTAL_ADDRESS.trim()) {
    // CAN-SPAM requires a postal address in every alert email; refuse to send without one.
    log("error", { where: "worker", reason: "POSTAL_ADDRESS is not set; not sending" });
    return { status: "blocked", digests: 0, sent: 0 };
  }

  const { users, followsByUser, sentByUser } = await loadAlertUsers(env);
  const digests = buildDigests(feed.events, users, followsByUser, sentByUser, nowDate);
  const cap = Math.max(0, Number(env.MAX_EMAILS_PER_RUN) || 0);
  const todo = digests.slice(0, cap);
  const ctx = { origin: env.APP_ORIGIN, from: env.EMAIL_FROM, unsubscribeSecret: env.UNSUBSCRIBE_SECRET, postalAddress: env.POSTAL_ADDRESS };

  let sent = 0;
  let failed = false;
  for (let i = 0; i < todo.length; i += BATCH_MAX) {
    const chunk = todo.slice(i, i + BATCH_MAX);
    const messages: Message[] = [];
    for (const d of chunk) messages.push(await renderDigest(d, ctx));
    if (dryRun) {
      // Dry run: log what would be sent, with the user ID in place of the address and the
      // unsubscribe token redacted.
      for (const [j, m] of messages.entries()) {
        const text = m.text.replace(/([?&#]t=)[A-Za-z0-9_-]+/g, "$1[redacted]");
        console.log(JSON.stringify({ level: "info", where: "worker", dry_run: true, user: chunk[j].user.id, subject: m.subject, text }));
      }
      sent += chunk.length;
      continue;
    }
    // Record first, then send, and roll back on failure: an event is never emailed twice,
    // and a failed send is retried on the next run.
    await env.DB.batch(sentRows(env, chunk, now));
    try {
      await sendBatch(env, messages);
      sent += chunk.length;
    } catch (err) {
      await env.DB.batch(unsentRows(env, chunk, now));
      log("error", { where: "worker", kind: err instanceof Error ? err.message : "send failed", count: chunk.length });
      failed = true;
      break;
    }
  }

  const complete = !failed && todo.length === digests.length;
  if (complete) {
    await env.DB.prepare("INSERT INTO worker_state (key, value) VALUES (?1, ?2) ON CONFLICT (key) DO UPDATE SET value = ?2")
      .bind(stateKey, feed.generatedAt)
      .run();
  }
  log("info", { where: "worker", dry_run: dryRun, generated_at: feed.generatedAt, events: feed.events.length, count: digests.length, sent, skipped: digests.length - sent });
  return { status: complete ? "complete" : "partial", digests: digests.length, sent };
}

export default {
  async scheduled(_event: ScheduledController, env: WorkerEnv, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(
      run(env).catch((err: unknown) => log("error", { where: "worker", kind: err instanceof Error ? err.message : "run failed" })),
    );
  },
};
