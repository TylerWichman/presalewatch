-- Accounts and email alerts. Times are Unix seconds (UTC).
-- Personal data kept to the minimum: an email address plus alert settings.

CREATE TABLE users (
  id TEXT PRIMARY KEY,                 -- random, never shown in URLs except signed unsubscribe links
  email TEXT NOT NULL UNIQUE,          -- validated and lowercased
  unsub_nonce TEXT NOT NULL,           -- per-user secret mixed into the unsubscribe signature
  created_at INTEGER NOT NULL
);

CREATE TABLE preferences (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  follow_alerts INTEGER NOT NULL DEFAULT 1 CHECK (follow_alerts IN (0, 1)),
  profit_alerts INTEGER NOT NULL DEFAULT 1 CHECK (profit_alerts IN (0, 1)),
  profit_threshold INTEGER NOT NULL DEFAULT 30 CHECK (profit_threshold BETWEEN 0 AND 500),
  updated_at INTEGER NOT NULL
);

CREATE TABLE follows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  artist TEXT NOT NULL,                -- as the user typed it (display only)
  artist_key TEXT NOT NULL,            -- normalized, used for matching
  created_at INTEGER NOT NULL,
  UNIQUE (user_id, artist_key)
);

-- Magic-link tokens. Only a SHA-256 hash of the token is stored.
CREATE TABLE login_tokens (
  token_hash TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER
);
CREATE INDEX login_tokens_email ON login_tokens (email);
CREATE INDEX login_tokens_expires ON login_tokens (expires_at);

-- Sessions. Only a SHA-256 hash of the session ID is stored.
CREATE TABLE sessions (
  id_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL          -- absolute expiry
);
CREATE INDEX sessions_user ON sessions (user_id);
CREATE INDEX sessions_expires ON sessions (expires_at);

-- One row per (user, event) ever emailed, so an event is never sent twice.
CREATE TABLE sent_alerts (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  event_id TEXT NOT NULL,
  sent_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, event_id)
);

-- Fixed-window counters. Keys are HMACs, so no raw IPs or emails are stored.
CREATE TABLE rate_limits (
  key TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY (key, window_start)
);

CREATE TABLE worker_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
