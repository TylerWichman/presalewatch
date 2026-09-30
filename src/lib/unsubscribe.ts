// Signed, per-user unsubscribe tokens. They work without signing in and can only affect their own user.

import { hmac, timingSafeEqual } from "./crypto.ts";

export const UNSUB_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
export const USER_ID_PATTERN = /^[A-Za-z0-9_-]{22}$/;

export function unsubscribeToken(secret: string, userId: string, nonce: string): Promise<string> {
  return hmac(secret, `unsubscribe:v1:${userId}:${nonce}`);
}

export async function checkUnsubscribeToken(secret: string, userId: string, nonce: string, token: string): Promise<boolean> {
  return timingSafeEqual(await unsubscribeToken(secret, userId, nonce), token);
}

/** Link for the email body: opens a confirm page, so link scanners can't unsubscribe anyone. */
export function unsubscribePageUrl(origin: string, userId: string, token: string): string {
  return `${origin}/unsubscribe#u=${userId}&t=${token}`;
}

/** RFC 8058 one-click target for the List-Unsubscribe header (mail providers POST here). */
export function oneClickUrl(origin: string, userId: string, token: string): string {
  return `${origin}/api/unsubscribe/one-click?u=${userId}&t=${token}`;
}
