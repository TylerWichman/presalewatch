// Runs before every /api/* route: CSRF checks, security headers, and a last-resort error handler.

import { errorResponse, json, securityHeaders, type Env } from "../../src/lib/http.ts";

const SAFE_METHODS = new Set(["GET", "HEAD"]);
// Mail providers POST here for RFC 8058 one-click unsubscribe, with no Origin header. The signed
// per-user token in the URL is the authorization, and it can only turn that user's alerts off.
export const ONE_CLICK_PATH = "/api/unsubscribe/one-click";

/** Strict same-origin check on every state-changing request, plus a JSON content type for bodies. */
export function csrfCheck(request: Request, env: Env): Response | null {
  if (SAFE_METHODS.has(request.method)) return null;
  if (new URL(request.url).pathname === ONE_CLICK_PATH) return null;
  if (request.headers.get("Origin") !== env.APP_ORIGIN) return json({ error: "Forbidden." }, 403);
  if (request.method !== "DELETE") {
    const type = (request.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();
    if (type !== "application/json") return json({ error: "Unsupported content type." }, 415);
  }
  return null;
}

interface MiddlewareCtx {
  request: Request;
  env: Env;
  next(): Promise<Response>;
}

export const onRequest = async (ctx: MiddlewareCtx): Promise<Response> => {
  try {
    const blocked = csrfCheck(ctx.request, ctx.env);
    if (blocked) return blocked;
    const res = await ctx.next();
    const out = new Response(res.body, res);
    securityHeaders(out.headers);
    return out;
  } catch (err) {
    return errorResponse(err, "middleware");
  }
};
