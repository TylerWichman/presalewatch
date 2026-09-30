// Server-side input validation. Every value from a request goes through here.

export class InputError extends Error {}

const EMAIL_PATTERN =
  /^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;

/** Lowercased ASCII address. The pattern has no room for CR, LF, spaces, commas, or angle brackets,
 * so a valid address can't inject email headers or extra recipients. */
export function email(value: unknown): string {
  if (typeof value !== "string" || value.length > 254) throw new InputError("Enter a valid email address.");
  const v = value.trim().toLowerCase();
  if (v.length > 254 || !EMAIL_PATTERN.test(v)) throw new InputError("Enter a valid email address.");
  return v;
}

export const ARTIST_MAX = 100;
export const FOLLOWS_MAX = 200;

/** A display name for an artist: normalized, trimmed, with control and format characters rejected. */
export function artist(value: unknown): string {
  if (typeof value !== "string" || value.length > ARTIST_MAX * 4) throw new InputError("Enter an artist name.");
  const v = value.normalize("NFC").replace(/\s+/g, " ").trim();
  if (!v || [...v].length > ARTIST_MAX) throw new InputError(`Artist names must be 1-${ARTIST_MAX} characters.`);
  if (/[\p{Cc}\p{Cf}\p{Co}\p{Cn}]/u.test(v)) throw new InputError("Artist name contains unsupported characters.");
  return v;
}

/** Matching key: accents, case, punctuation, and "&" vs "and" don't matter. */
export function artistKey(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

export const THRESHOLD_MIN = 0;
export const THRESHOLD_MAX = 500;

export function threshold(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < THRESHOLD_MIN || value > THRESHOLD_MAX) {
    throw new InputError(`Profit threshold must be a whole number from ${THRESHOLD_MIN} to ${THRESHOLD_MAX}.`);
  }
  return value;
}

export function bool(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") throw new InputError(`${name} must be true or false.`);
  return value;
}

export function followId(value: string | undefined): number {
  if (!value || !/^[1-9][0-9]{0,15}$/.test(value)) throw new InputError("Not found.");
  return Number(value);
}

/** Post-login destinations. Anything else falls back to /alerts, so there is no open redirect. */
export const REDIRECTS = ["/", "/alerts"] as const;

export function redirectPath(value: unknown): string {
  return typeof value === "string" && (REDIRECTS as readonly string[]).includes(value) ? value : "/alerts";
}

export function token(value: unknown, pattern: RegExp): string {
  if (typeof value !== "string" || !pattern.test(value)) throw new InputError("This link is invalid or has expired.");
  return value;
}
