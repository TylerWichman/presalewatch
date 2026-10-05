-- Sign-in by link or 6-digit code (Login & Email Experience spec, PR 1).
-- One login_tokens row is one sign-in request: the link token and the code share it, so using
-- either one (used_at) ends both. See README (Accounts and alerts > Security).

ALTER TABLE login_tokens ADD COLUMN code_hash TEXT;                        -- keyed hash (HMAC) of the code, bound to this request
ALTER TABLE login_tokens ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;   -- code tries; the request is dead at 5
ALTER TABLE login_tokens ADD COLUMN request_binding_hash TEXT;             -- SHA-256 of the pending-login cookie of the browser that asked
ALTER TABLE login_tokens ADD COLUMN created_at INTEGER;                    -- Unix seconds; resend waits 30 seconds after it

CREATE INDEX login_tokens_binding ON login_tokens (request_binding_hash);
