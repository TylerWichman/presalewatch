// Email rendering and sending through Resend. Everything from event data or users is escaped.

import type { Digest, FeedEvent } from "./match.ts";
import { edgeOf } from "./match.ts";
import { oneClickUrl, unsubscribePageUrl, unsubscribeToken } from "./unsubscribe.ts";

const RESEND_URL = "https://api.resend.com/emails";
const RESEND_BATCH_URL = "https://api.resend.com/emails/batch";
export const BATCH_MAX = 100;

export interface Message {
  from: string;
  to: string[];
  subject: string;
  html: string;
  text: string;
  headers?: Record<string, string>;
}

export interface MailEnv {
  RESEND_API_KEY: string;
  EMAIL_FROM: string;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

const LINK_HOSTS = ["ticketmaster.com", "livenation.com"];

/** Only https links to Ticketmaster or Live Nation go into emails; anything else is dropped. */
export function safeEventUrl(url: string | null): string | null {
  if (!url) return null;
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  if (u.protocol !== "https:" || u.username || u.password) return null;
  const host = u.hostname.toLowerCase();
  return LINK_HOSTS.some((h) => host === h || host.endsWith(`.${h}`)) ? u.href : null;
}

/** Header values are built from fixed text and validated addresses; this is a last line of defense. */
export function assertHeaderSafe(...values: string[]): void {
  for (const v of values) if (/[\r\n\0]/.test(v)) throw new Error("header value contains a line break");
}

function pct(f: number): string {
  const n = Math.round(f * 100);
  return (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n) + "%";
}

function edgeText(ev: FeedEvent): string {
  if (ev.mode === "live") return `${pct(ev.profit ?? 0)} Profit %, based on asking prices`;
  if (ev.profitLow === null || ev.profitHigh === null) return "Not rated: no artist listening data";
  return `Estimate: ${pct(ev.profitLow ?? 0)} to ${pct(ev.profitHigh ?? 0)} (midpoint ${pct(edgeOf(ev))}), ${ev.tier ?? "unknown"} demand`;
}

function whenWhere(ev: FeedEvent): string {
  const date = ev.date
    ? new Date(`${ev.date}T12:00:00Z`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "UTC" })
    : "Date TBA";
  const place = [ev.venue, [ev.city, ev.state].filter(Boolean).join(", ")].filter(Boolean).join(" · ");
  return place ? `${date} · ${place}` : date;
}

function title(ev: FeedEvent): string {
  return ev.name && ev.name.toLowerCase() !== ev.artist.toLowerCase() ? `${ev.artist}: ${ev.name}` : ev.artist;
}

function htmlItem(ev: FeedEvent, origin: string): string {
  const href = safeEventUrl(ev.url) ?? `${origin}/`;
  return `<li style="margin:0 0 14px">
<a href="${escapeHtml(href)}" style="color:#6d28d9;font-weight:600">${escapeHtml(title(ev))}</a><br>
<span style="color:#555">${escapeHtml(whenWhere(ev))}</span><br>
<span>${escapeHtml(edgeText(ev))}</span>
</li>`;
}

function textItem(ev: FeedEvent, origin: string): string {
  return `- ${title(ev)}\n  ${whenWhere(ev)}\n  ${edgeText(ev)}\n  ${safeEventUrl(ev.url) ?? `${origin}/`}`;
}

export interface DigestContext {
  origin: string;
  from: string;
  unsubscribeSecret: string;
  postalAddress: string;
}

export async function renderDigest(d: Digest, ctx: DigestContext): Promise<Message> {
  const token = await unsubscribeToken(ctx.unsubscribeSecret, d.user.id, d.user.unsub_nonce);
  const unsubPage = unsubscribePageUrl(ctx.origin, d.user.id, token);
  const oneClick = oneClickUrl(ctx.origin, d.user.id, token);
  const count = d.profit.length + d.follows.length;
  const subject = `PouchIt: ${count} new presale alert${count === 1 ? "" : "s"}`;
  const hasEstimates = d.profit.some((e) => e.mode !== "live") || d.follows.some((e) => e.mode !== "live");

  const htmlSections: string[] = [];
  const textSections: string[] = [];
  if (d.follows.length) {
    htmlSections.push(`<h2 style="font-size:16px">Artists you follow</h2><ul style="padding-left:18px">${d.follows.map((e) => htmlItem(e, ctx.origin)).join("")}</ul>`);
    textSections.push(`ARTISTS YOU FOLLOW\n\n${d.follows.map((e) => textItem(e, ctx.origin)).join("\n\n")}`);
  }
  if (d.profit.length) {
    const head = `High profit (${d.user.profit_threshold}% or more)`;
    htmlSections.push(`<h2 style="font-size:16px">${escapeHtml(head)}</h2><ul style="padding-left:18px">${d.profit.map((e) => htmlItem(e, ctx.origin)).join("")}</ul>`);
    textSections.push(`${head.toUpperCase()}\n\n${d.profit.map((e) => textItem(e, ctx.origin)).join("\n\n")}`);
  }
  const estimateNote = hasEstimates
    ? "Items marked Estimate are predictions from demand signals, not live resale prices."
    : "";
  const footer = [
    "You get this email because you turned on alerts at PouchIt.",
    `Manage alerts: ${ctx.origin}/alerts`,
    `Unsubscribe: ${unsubPage}`,
    ctx.postalAddress,
  ];

  const html = `<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.45;color:#111;max-width:600px">
<h1 style="font-size:18px">New presale alerts</h1>
${htmlSections.join("\n")}
${estimateNote ? `<p style="color:#555">${escapeHtml(estimateNote)}</p>` : ""}
<p style="color:#555;font-size:12px;border-top:1px solid #ddd;padding-top:12px">
${escapeHtml(footer[0])}<br>
<a href="${escapeHtml(`${ctx.origin}/alerts`)}">Manage alerts</a> · <a href="${escapeHtml(unsubPage)}">Unsubscribe</a><br>
${escapeHtml(ctx.postalAddress)}<br>
Not financial advice. Always confirm prices on the ticketing site.
</p></body></html>`;
  const text = `New presale alerts\n\n${textSections.join("\n\n")}\n\n${estimateNote}\n\n--\n${footer.join("\n")}\nNot financial advice. Always confirm prices on the ticketing site.\n`;

  assertHeaderSafe(d.user.email, subject, oneClick, ctx.from);
  return {
    from: ctx.from,
    to: [d.user.email],
    subject,
    html,
    text,
    headers: {
      "List-Unsubscribe": `<${oneClick}>`,
      "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    },
  };
}

/** Short and plain on purpose: no images, buttons, or styling, which reads as a personal
 * transactional message to spam filters. The HTML part is the text with the link clickable. */
export function renderSignIn(from: string, to: string, link: string, code: string): Message {
  if (!/^[0-9]{6}$/.test(code)) throw new Error("sign-in code must be 6 digits");
  const subject = "Your PouchIt sign-in link";
  assertHeaderSafe(from, to, subject);
  const lines = [
    `Your sign-in code: ${code}`,
    "Or sign in with this link:",
    link,
    "The code and link work once and expire in 15 minutes. If you didn't ask for this, ignore this email.",
  ];
  const text = `${lines[0]}\n\n${lines[1]}\n${lines[2]}\n\n${lines[3]}\n`;
  const html = `<!doctype html><html><body>
<p>${escapeHtml("Your sign-in code:")} <b>${escapeHtml(code)}</b></p>
<p>${escapeHtml(lines[1])}<br><a href="${escapeHtml(link)}">${escapeHtml(link)}</a></p>
<p>${escapeHtml(lines[3])}</p>
</body></html>`;
  return { from, to: [to], subject, html, text };
}

async function post(env: MailEnv, url: string, body: unknown): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // The response body can echo request details, so only the status is surfaced.
  if (!res.ok) throw new Error(`Resend returned HTTP ${res.status}`);
}

export function sendEmail(env: MailEnv, message: Message): Promise<void> {
  return post(env, RESEND_URL, message);
}

export function sendBatch(env: MailEnv, messages: Message[]): Promise<void> {
  if (messages.length > BATCH_MAX) throw new Error("batch too large");
  return post(env, RESEND_BATCH_URL, messages);
}
