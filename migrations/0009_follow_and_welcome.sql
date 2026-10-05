-- Follow from an event card, and the welcome email (Login & Email Experience spec, PR 3).

-- The artist a signed-out visitor asked to follow ("Alert me"), kept with their sign-in request
-- and followed once they sign in. Validated like any follow (1-100 characters, no control chars).
ALTER TABLE login_tokens ADD COLUMN pending_follow TEXT;

-- When the one-time welcome email went out. NULL means not yet.
ALTER TABLE users ADD COLUMN welcomed_at INTEGER;

-- Accounts that already exist signed up before the welcome email did: don't send them one now.
UPDATE users SET welcomed_at = created_at;
