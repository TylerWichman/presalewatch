// Small Web Crypto helpers. Work the same in Workers and Node 24 (tests).

const encoder = new TextEncoder();

export function base64url(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** URL-safe random token. 32 bytes = 256 bits, 43 characters. */
export function randomToken(bytes = 32): string {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

export async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(text));
  return base64url(new Uint8Array(digest));
}

export async function hmac(secret: string, message: string): Promise<string> {
  if (!secret || secret.length < 32) throw new Error("HMAC secret missing or too short");
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return base64url(new Uint8Array(sig));
}

/** Constant-time string comparison (length is not secret here). */
export function timingSafeEqual(a: string, b: string): boolean {
  const x = encoder.encode(a);
  const y = encoder.encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

export const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
