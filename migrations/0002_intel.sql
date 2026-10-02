-- Artist & venue intelligence database. See docs/database.md for every field.
--
-- Conventions:
--   * Times are ISO-8601 UTC text ('2026-10-02T12:34:57Z'); local event dates are 'YYYY-MM-DD'.
--   * Every table row records `source` (where it came from) and `last_updated`.
--   * Missing data stays NULL. Nothing is estimated or filled in at this layer.
--   * External IDs are UNIQUE, so ingestion can upsert on them (idempotent).

-- ---- Reference tables (maintained by hand) ----------------------------------------

CREATE TABLE metros (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,                 -- e.g. 'New York-Newark-Jersey City'
  cbsa_code TEXT UNIQUE,                     -- US Census metro (CBSA) code
  population INTEGER CHECK (population > 0),
  population_year INTEGER,
  ticketmaster_market_ids TEXT,              -- ';'-separated Ticketmaster market IDs in this metro
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL
);

CREATE TABLE state_resale_rules (
  state TEXT PRIMARY KEY CHECK (length(state) = 2),   -- 'NY'
  has_price_cap INTEGER CHECK (has_price_cap IN (0, 1)),
  price_cap_note TEXT,                       -- plain-English summary of any cap
  restricts_transfer INTEGER CHECK (restricts_transfer IN (0, 1)),  -- may sellers make tickets non-transferable?
  transfer_note TEXT,
  law_reference TEXT,                        -- statute or source link
  reviewed_on TEXT,                          -- date a person last checked this row
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL
);

-- ---- Artists -----------------------------------------------------------------------

CREATE TABLE artists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,                    -- normalized name used for matching
  mbid TEXT UNIQUE,                          -- MusicBrainz artist ID
  ticketmaster_id TEXT UNIQUE,               -- Ticketmaster attraction ID
  seatgeek_id TEXT UNIQUE,                   -- nullable until SeatGeek partner access
  lastfm_name TEXT,
  lastfm_listeners INTEGER CHECK (lastfm_listeners >= 0),   -- latest value; history in artist_metrics_snapshots
  lastfm_playcount INTEGER CHECK (lastfm_playcount >= 0),
  lastfm_checked_at TEXT,
  active_from INTEGER,                       -- year the artist started (MusicBrainz)
  active_to INTEGER,                         -- year the artist ended, NULL if active or unknown
  musicbrainz_checked_at TEXT,
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL
);
CREATE INDEX artists_name_key ON artists (name_key);

CREATE TABLE artist_tags (
  artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,                         -- lowercased, e.g. 'indie rock'
  rank INTEGER NOT NULL,                     -- 1 = most applied tag
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  PRIMARY KEY (artist_id, tag, source)
);

CREATE TABLE artist_similar (
  artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
  similar_name TEXT NOT NULL,
  similar_mbid TEXT,
  similar_artist_id INTEGER REFERENCES artists(id) ON DELETE SET NULL,  -- set when we track that artist too
  match REAL CHECK (match BETWEEN 0 AND 1),  -- Last.fm similarity score
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  PRIMARY KEY (artist_id, similar_name, source)
);

-- One row per artist per day per source; momentum compares these over time.
CREATE TABLE artist_metrics_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
  captured_on TEXT NOT NULL,                 -- 'YYYY-MM-DD'
  lastfm_listeners INTEGER CHECK (lastfm_listeners >= 0),
  lastfm_playcount INTEGER CHECK (lastfm_playcount >= 0),
  seatgeek_score REAL,                       -- nullable until SeatGeek partner access
  seatgeek_popularity REAL,
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  UNIQUE (artist_id, captured_on, source)
);
CREATE INDEX artist_metrics_by_date ON artist_metrics_snapshots (artist_id, captured_on);

-- ---- Venues ------------------------------------------------------------------------

CREATE TABLE venues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,
  ticketmaster_id TEXT UNIQUE,
  seatgeek_id TEXT UNIQUE,
  wikidata_id TEXT UNIQUE,
  address TEXT,
  city TEXT,
  state TEXT,                                -- two-letter code
  postal_code TEXT,
  country TEXT,
  latitude REAL CHECK (latitude BETWEEN -90 AND 90),
  longitude REAL CHECK (longitude BETWEEN -180 AND 180),
  metro_id INTEGER REFERENCES metros(id) ON DELETE SET NULL,
  capacity INTEGER CHECK (capacity > 0),     -- concert capacity
  capacity_source TEXT,                      -- 'manual', 'wikipedia', 'wikidata', ...
  capacity_source_url TEXT,
  capacity_note TEXT,                        -- e.g. 'concerts; 6,500 for basketball'
  capacity_verified INTEGER NOT NULL DEFAULT 0 CHECK (capacity_verified IN (0, 1)),  -- a person checked it
  venue_type TEXT CHECK (venue_type IN ('club', 'theater', 'amphitheater', 'arena', 'stadium', 'festival', 'other')),
  primary_platform TEXT,                     -- 'ticketmaster', 'axs', ...
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL
);
CREATE INDEX venues_name_key ON venues (name_key, state);
CREATE INDEX venues_metro ON venues (metro_id);

-- ---- Events and presales -------------------------------------------------------------
-- Rows are never deleted, so past dates accumulate into tour history.

CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticketmaster_id TEXT UNIQUE,
  seatgeek_id TEXT UNIQUE,
  name TEXT NOT NULL,
  tour_name TEXT,
  venue_id INTEGER REFERENCES venues(id),
  event_date TEXT,                           -- local date, NULL if TBA
  event_time TEXT,                           -- local 'HH:MM:SS', NULL if TBA
  starts_at TEXT,                            -- UTC, when known
  status TEXT CHECK (status IN ('onsale', 'offsale', 'cancelled', 'postponed', 'rescheduled', 'unknown')),
  sold_out INTEGER CHECK (sold_out IN (0, 1)),   -- only set when a source says so; NULL = unknown
  face_min REAL CHECK (face_min >= 0),
  face_max REAL CHECK (face_max >= 0),
  face_currency TEXT,
  face_fee_included INTEGER CHECK (face_fee_included IN (0, 1)),
  dynamic_pricing INTEGER CHECK (dynamic_pricing IN (0, 1)),
  onsale_at TEXT,                            -- public on-sale start, UTC
  ticket_limit INTEGER CHECK (ticket_limit > 0),
  url TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,                -- last time a source still listed it
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL
);
CREATE INDEX events_venue_date ON events (venue_id, event_date);
CREATE INDEX events_date ON events (event_date);

CREATE TABLE event_artists (
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  artist_id INTEGER NOT NULL REFERENCES artists(id),
  position INTEGER NOT NULL,                 -- 0 = headliner, then billing order
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  PRIMARY KEY (event_id, artist_id)
);
CREATE INDEX event_artists_artist ON event_artists (artist_id, position);

CREATE TABLE presales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  presale_type TEXT NOT NULL,                -- 'Artist', 'Amex', 'Citi', 'Venue', 'Live Nation', ...
  starts_at TEXT NOT NULL,
  ends_at TEXT NOT NULL,
  access_requirements TEXT,                  -- how to get access / where the code comes from
  description TEXT,
  url TEXT,
  link_text TEXT,
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  UNIQUE (event_id, name, starts_at)
);
CREATE INDEX presales_window ON presales (starts_at, ends_at);

-- ---- Prices --------------------------------------------------------------------------

-- Append-only resale time series from APIs. Rows can be deleted (to honor a source's
-- retention terms) but never edited.
CREATE TABLE resale_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  captured_at TEXT NOT NULL,
  lowest REAL CHECK (lowest >= 0),
  median REAL CHECK (median >= 0),
  average REAL CHECK (average >= 0),
  listing_count INTEGER CHECK (listing_count >= 0),
  price_basis TEXT NOT NULL DEFAULT 'ask' CHECK (price_basis IN ('ask', 'sold')),
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  UNIQUE (event_id, source, captured_at)
);
CREATE INDEX resale_snapshots_event ON resale_snapshots (event_id, captured_at);
CREATE TRIGGER resale_snapshots_append_only BEFORE UPDATE ON resale_snapshots
BEGIN
  SELECT RAISE(ABORT, 'resale_snapshots is append-only');
END;

-- Prices logged by hand (CSV import): face values and resale asks or sales.
CREATE TABLE observed_prices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,  -- NULL until the row is matched
  event_ref TEXT NOT NULL,                   -- what the CSV said (Ticketmaster event ID or URL)
  kind TEXT NOT NULL CHECK (kind IN ('face', 'resale')),
  price_basis TEXT CHECK (price_basis IN ('ask', 'sold')),   -- resale only: listing price or sale price
  section_tier TEXT,                         -- e.g. 'GA floor', 'Sec 104', 'Platinum'
  standard_ticket INTEGER NOT NULL DEFAULT 1 CHECK (standard_ticket IN (0, 1)),  -- 0 for VIP/Platinum
  price REAL NOT NULL CHECK (price > 0),     -- per ticket
  fees_included INTEGER CHECK (fees_included IN (0, 1)),
  currency TEXT NOT NULL DEFAULT 'USD',
  observed_on TEXT NOT NULL,                 -- 'YYYY-MM-DD'
  source TEXT NOT NULL,                      -- where the price was seen, e.g. 'StubHub listing'
  notes TEXT,
  import_batch TEXT NOT NULL,                -- CSV file name + import time
  last_updated TEXT NOT NULL,
  CHECK (kind = 'face' OR price_basis IS NOT NULL)
);
CREATE INDEX observed_prices_event ON observed_prices (event_id, kind);

-- ---- Matching and run bookkeeping ----------------------------------------------------

-- Records that couldn't be matched automatically, and capacity drafts, for a person to review.
CREATE TABLE match_review (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('artist_match', 'venue_match', 'event_match', 'venue_capacity', 'observed_price')),
  source TEXT NOT NULL,
  external_id TEXT,
  external_name TEXT,
  details TEXT,                              -- JSON with whatever the reviewer needs
  candidate_id INTEGER,                      -- best guess in our tables, if any
  candidate_score REAL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'rejected')),
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution_note TEXT,
  last_updated TEXT NOT NULL,
  UNIQUE (kind, source, external_id)
);
CREATE INDEX match_review_open ON match_review (status, kind);

CREATE TABLE ingest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed')),
  api_calls INTEGER,
  rows_written INTEGER,
  note TEXT
);

-- ---- Derived features (views) --------------------------------------------------------
-- Fees, the ask-to-sale discount, and the Profit % formula live in config/model.json and
-- edge.py, so these views return raw inputs and leave the money math to one place.

-- Each event's headliner.
CREATE VIEW v_event_headliner AS
SELECT ea.event_id, ea.artist_id
FROM event_artists ea
WHERE ea.position = 0;

-- Where an event is, as a market key: its metro when known, else its city.
CREATE VIEW v_event_place AS
SELECT e.id AS event_id, e.event_date, e.status, e.tour_name, e.venue_id, v.state, v.venue_type, v.capacity,
       CASE WHEN v.metro_id IS NOT NULL THEN 'metro:' || v.metro_id
            WHEN v.city IS NOT NULL THEN 'city:' || lower(v.state) || ':' || lower(v.city)
       END AS market_key
FROM events e LEFT JOIN venues v ON v.id = e.venue_id;

-- Listener growth from dated snapshots: latest value vs the closest snapshot at least 30 / 90 days older
-- (and no more than 30 days older than that, so a stale snapshot doesn't pose as a 30-day change).
CREATE VIEW v_artist_momentum AS
WITH latest AS (
  SELECT artist_id, MAX(captured_on) AS captured_on
  FROM artist_metrics_snapshots WHERE lastfm_listeners IS NOT NULL GROUP BY artist_id
)
SELECT l.artist_id, l.captured_on AS latest_on, s.lastfm_listeners AS listeners,
  (SELECT p.lastfm_listeners FROM artist_metrics_snapshots p
    WHERE p.artist_id = l.artist_id AND p.lastfm_listeners IS NOT NULL
      AND julianday(p.captured_on) <= julianday(l.captured_on) - 30
      AND julianday(p.captured_on) >= julianday(l.captured_on) - 60
    ORDER BY p.captured_on DESC LIMIT 1) AS listeners_30d_ago,
  (SELECT p.lastfm_listeners FROM artist_metrics_snapshots p
    WHERE p.artist_id = l.artist_id AND p.lastfm_listeners IS NOT NULL
      AND julianday(p.captured_on) <= julianday(l.captured_on) - 90
      AND julianday(p.captured_on) >= julianday(l.captured_on) - 120
    ORDER BY p.captured_on DESC LIMIT 1) AS listeners_90d_ago
FROM latest l
JOIN artist_metrics_snapshots s ON s.artist_id = l.artist_id AND s.captured_on = l.captured_on AND s.lastfm_listeners IS NOT NULL
GROUP BY l.artist_id;

-- Demand: the headliner's listeners and momentum against the venue's capacity.
CREATE VIEW v_event_demand AS
SELECT h.event_id, h.artist_id, a.lastfm_listeners AS listeners, a.lastfm_playcount AS playcount,
  CASE WHEN a.lastfm_listeners > 0 THEN 1.0 * a.lastfm_playcount / a.lastfm_listeners END AS plays_per_listener,
  CASE WHEN m.listeners_30d_ago > 0 THEN 1.0 * m.listeners / m.listeners_30d_ago - 1 END AS growth_30d,
  CASE WHEN m.listeners_90d_ago > 0 THEN 1.0 * m.listeners / m.listeners_90d_ago - 1 END AS growth_90d,
  p.capacity,
  CASE WHEN p.capacity > 0 AND a.lastfm_listeners IS NOT NULL THEN 1.0 * a.lastfm_listeners / p.capacity END AS listeners_per_seat
FROM v_event_headliner h
JOIN artists a ON a.id = h.artist_id
JOIN v_event_place p ON p.event_id = h.event_id
LEFT JOIN v_artist_momentum m ON m.artist_id = h.artist_id;

-- Tour history: every dated, non-cancelled date we've seen for each headliner.
CREATE VIEW tour_history AS
SELECT h.artist_id, p.event_id, p.event_date, p.tour_name, p.venue_id, p.market_key, p.state, p.status
FROM v_event_headliner h
JOIN v_event_place p ON p.event_id = h.event_id
WHERE p.event_date IS NOT NULL AND (p.status IS NULL OR p.status <> 'cancelled');

-- Scarcity. A tour is the artist's dates within 90 days of this one.
--   tour_dates: the artist's dates within ±90 days, anywhere.
--   dates_in_market_on_tour: of those, how many are in this event's metro (or city).
--   days_since_last_in_market: days back to the artist's last date in this market before
--     this tour (more than 90 days earlier). NULL when we have no such date on record.
--   dates_last_365d: the artist's dates in the year before this one (tour frequency).
-- History only exists from when ingestion started, so these undercount at first.
CREATE VIEW v_event_scarcity AS
SELECT t.event_id, t.artist_id,
  (SELECT COUNT(*) FROM tour_history o WHERE o.artist_id = t.artist_id
     AND abs(julianday(o.event_date) - julianday(t.event_date)) <= 90) AS tour_dates,
  (SELECT COUNT(*) FROM tour_history o WHERE o.artist_id = t.artist_id AND o.market_key = t.market_key
     AND abs(julianday(o.event_date) - julianday(t.event_date)) <= 90) AS dates_in_market_on_tour,
  (SELECT CAST(julianday(t.event_date) - julianday(MAX(o.event_date)) AS INTEGER) FROM tour_history o
     WHERE o.artist_id = t.artist_id AND o.market_key = t.market_key
       AND julianday(o.event_date) < julianday(t.event_date) - 90) AS days_since_last_in_market,
  (SELECT COUNT(*) FROM tour_history o WHERE o.artist_id = t.artist_id
     AND julianday(o.event_date) < julianday(t.event_date)
     AND julianday(o.event_date) >= julianday(t.event_date) - 365) AS dates_last_365d
FROM tour_history t;

-- Medians of manually observed prices per event, standard tickets only.
CREATE VIEW v_observed_medians AS
WITH o AS (
  SELECT event_id, kind, COALESCE(price_basis, 'face') AS basis, price,
    ROW_NUMBER() OVER (PARTITION BY event_id, kind, price_basis ORDER BY price) AS rn,
    COUNT(*) OVER (PARTITION BY event_id, kind, price_basis) AS n,
    MAX(observed_on) OVER (PARTITION BY event_id, kind, price_basis) AS latest_on
  FROM observed_prices
  WHERE event_id IS NOT NULL AND standard_ticket = 1
)
SELECT event_id, kind, basis, n AS observations, latest_on, AVG(price) AS median_price
FROM o WHERE rn IN ((n + 1) / 2, (n + 2) / 2)
GROUP BY event_id, kind, basis, n, latest_on;

-- One face price and one resale price per event, API data first, manual observations when the
-- API has none. Resale prefers sale prices over asks among observations.
CREATE VIEW v_event_prices AS
WITH snap AS (
  SELECT s.event_id, s.median, s.price_basis, s.source, s.captured_at,
    ROW_NUMBER() OVER (PARTITION BY s.event_id ORDER BY s.captured_at DESC) AS rn
  FROM resale_snapshots s WHERE s.median IS NOT NULL
)
SELECT e.id AS event_id,
  COALESCE(CASE WHEN e.face_min IS NOT NULL AND e.face_max IS NOT NULL THEN (e.face_min + e.face_max) / 2
                ELSE COALESCE(e.face_min, e.face_max) END, fo.median_price) AS face,
  CASE WHEN e.face_min IS NOT NULL OR e.face_max IS NOT NULL THEN e.source
       WHEN fo.median_price IS NOT NULL THEN 'observed' END AS face_source,
  e.face_fee_included,
  COALESCE(sn.median, rs.median_price, ra.median_price) AS resale,
  CASE WHEN sn.median IS NOT NULL THEN sn.price_basis
       WHEN rs.median_price IS NOT NULL THEN 'sold'
       WHEN ra.median_price IS NOT NULL THEN 'ask' END AS resale_basis,
  CASE WHEN sn.median IS NOT NULL THEN sn.source
       WHEN rs.median_price IS NOT NULL OR ra.median_price IS NOT NULL THEN 'observed' END AS resale_source,
  COALESCE(sn.captured_at, rs.latest_on, ra.latest_on) AS resale_as_of
FROM events e
LEFT JOIN snap sn ON sn.event_id = e.id AND sn.rn = 1
LEFT JOIN v_observed_medians fo ON fo.event_id = e.id AND fo.kind = 'face'
LEFT JOIN v_observed_medians rs ON rs.event_id = e.id AND rs.kind = 'resale' AND rs.basis = 'sold'
LEFT JOIN v_observed_medians ra ON ra.event_id = e.id AND ra.kind = 'resale' AND ra.basis = 'ask';

-- Resale markup (resale ÷ face) per event where both prices exist. Raw: no fees, no ask discount.
CREATE VIEW v_event_markup AS
SELECT ep.event_id, ep.face, ep.resale, ep.resale_basis, ep.resale / ep.face AS markup
FROM v_event_prices ep
WHERE ep.face > 0 AND ep.resale IS NOT NULL;

-- Venue premium: median markup per venue, separately for asks and sales.
CREATE VIEW v_venue_premium AS
WITH m AS (
  SELECT e.venue_id, k.resale_basis, k.markup,
    ROW_NUMBER() OVER (PARTITION BY e.venue_id, k.resale_basis ORDER BY k.markup) AS rn,
    COUNT(*) OVER (PARTITION BY e.venue_id, k.resale_basis) AS n
  FROM v_event_markup k JOIN events e ON e.id = k.event_id
  WHERE e.venue_id IS NOT NULL
)
SELECT venue_id, resale_basis, n AS events, AVG(markup) AS median_markup
FROM m WHERE rn IN ((n + 1) / 2, (n + 2) / 2)
GROUP BY venue_id, resale_basis, n;

-- Venue-type premium: the same, per venue type.
CREATE VIEW v_venue_type_premium AS
WITH m AS (
  SELECT v.venue_type, k.resale_basis, k.markup,
    ROW_NUMBER() OVER (PARTITION BY v.venue_type, k.resale_basis ORDER BY k.markup) AS rn,
    COUNT(*) OVER (PARTITION BY v.venue_type, k.resale_basis) AS n
  FROM v_event_markup k JOIN events e ON e.id = k.event_id JOIN venues v ON v.id = e.venue_id
  WHERE v.venue_type IS NOT NULL
)
SELECT venue_type, resale_basis, n AS events, AVG(markup) AS median_markup
FROM m WHERE rn IN ((n + 1) / 2, (n + 2) / 2)
GROUP BY venue_type, resale_basis, n;

-- Artist premium: median markup per headliner, plus sellout rate over events with a known answer.
CREATE VIEW v_artist_premium AS
WITH m AS (
  SELECT h.artist_id, k.resale_basis, k.markup,
    ROW_NUMBER() OVER (PARTITION BY h.artist_id, k.resale_basis ORDER BY k.markup) AS rn,
    COUNT(*) OVER (PARTITION BY h.artist_id, k.resale_basis) AS n
  FROM v_event_markup k JOIN v_event_headliner h ON h.event_id = k.event_id
),
med AS (
  SELECT artist_id, resale_basis, n, AVG(markup) AS median_markup
  FROM m WHERE rn IN ((n + 1) / 2, (n + 2) / 2)
  GROUP BY artist_id, resale_basis, n
),
sell AS (
  SELECT h.artist_id, COUNT(e.sold_out) AS known, AVG(e.sold_out) AS sellout_rate
  FROM v_event_headliner h JOIN events e ON e.id = h.event_id
  GROUP BY h.artist_id
)
SELECT a.id AS artist_id, med.resale_basis, COALESCE(med.n, 0) AS events_with_markup, med.median_markup,
  sell.known AS events_with_sellout_known, CASE WHEN sell.known > 0 THEN sell.sellout_rate END AS sellout_rate
FROM artists a
LEFT JOIN med ON med.artist_id = a.id
LEFT JOIN sell ON sell.artist_id = a.id;

-- Market reference for each venue: metro population plus state resale rules (the spec's venue_market).
CREATE VIEW venue_market AS
SELECT v.id AS venue_id, v.metro_id, mt.name AS metro_name, mt.population AS metro_population,
  v.state, r.has_price_cap, r.price_cap_note, r.restricts_transfer, r.transfer_note
FROM venues v
LEFT JOIN metros mt ON mt.id = v.metro_id
LEFT JOIN state_resale_rules r ON r.state = v.state;

-- Everything scoring needs, one row per event.
CREATE VIEW v_event_features AS
SELECT e.id AS event_id, e.ticketmaster_id, e.name, e.event_date, e.status, e.venue_id,
  d.artist_id, d.listeners, d.playcount, d.plays_per_listener, d.growth_30d, d.growth_90d,
  d.capacity, d.listeners_per_seat,
  s.tour_dates, s.dates_in_market_on_tour, s.days_since_last_in_market, s.dates_last_365d,
  vm.metro_population, vm.has_price_cap, vm.restricts_transfer,
  pr.face, pr.face_source, pr.face_fee_included, pr.resale, pr.resale_basis, pr.resale_source, pr.resale_as_of
FROM events e
LEFT JOIN v_event_demand d ON d.event_id = e.id
LEFT JOIN v_event_scarcity s ON s.event_id = e.id
LEFT JOIN venue_market vm ON vm.venue_id = e.venue_id
LEFT JOIN v_event_prices pr ON pr.event_id = e.id;
