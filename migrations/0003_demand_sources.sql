-- Free demand sources: Wikidata IDs, Wikipedia pageviews, ListenBrainz, MusicBrainz details,
-- YouTube (current values only), and Ticketmaster status history. See docs/database.md.

-- ---- Artist identity and details ---------------------------------------------------

ALTER TABLE artists ADD COLUMN wikidata_id TEXT;                -- e.g. 'Q44190'
ALTER TABLE artists ADD COLUMN wikipedia_title TEXT;            -- English Wikipedia article title
ALTER TABLE artists ADD COLUMN youtube_channel_id TEXT;         -- official channel, from Wikidata (P2397)
ALTER TABLE artists ADD COLUMN artist_type TEXT;                -- MusicBrainz: Person, Group, Orchestra, Choir, Character, Other
ALTER TABLE artists ADD COLUMN country TEXT;                    -- MusicBrainz ISO country code
ALTER TABLE artists ADD COLUMN mbid_source TEXT;                -- how the MBID was resolved: 'ticketmaster', 'lastfm', 'musicbrainz_search', 'manual'
ALTER TABLE artists ADD COLUMN wikidata_checked_at TEXT;
ALTER TABLE artists ADD COLUMN listenbrainz_checked_at TEXT;
ALTER TABLE artists ADD COLUMN pageviews_checked_at TEXT;
CREATE UNIQUE INDEX artists_wikidata_id ON artists (wikidata_id);
ALTER TABLE venues ADD COLUMN wikidata_checked_at TEXT;

CREATE TABLE artist_aliases (
  artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  alias_key TEXT NOT NULL,                   -- normalized, for matching
  locale TEXT,
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  PRIMARY KEY (artist_id, alias, source)
);
CREATE INDEX artist_aliases_key ON artist_aliases (alias_key);

-- ---- Daily demand signals ----------------------------------------------------------

-- ListenBrainz totals ride in the existing per-day, per-source snapshot table.
ALTER TABLE artist_metrics_snapshots ADD COLUMN listenbrainz_listeners INTEGER CHECK (listenbrainz_listeners >= 0);  -- total_user_count
ALTER TABLE artist_metrics_snapshots ADD COLUMN listenbrainz_listens INTEGER CHECK (listenbrainz_listens >= 0);      -- total_listen_count

-- Daily English Wikipedia views of the artist's article (human traffic only).
CREATE TABLE artist_pageviews (
  artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
  day TEXT NOT NULL,                         -- 'YYYY-MM-DD'
  views INTEGER NOT NULL CHECK (views >= 0),
  article TEXT NOT NULL,                     -- the title the views were counted for
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  PRIMARY KEY (artist_id, day)
);

-- YouTube channel statistics: CURRENT VALUES ONLY. YouTube's developer policies (III.E.4.d)
-- allow storing these for at most 30 days, so each refresh overwrites the row and ingestion
-- deletes any row older than 30 days. No history, and no metrics derived from them (III.E.4.h).
CREATE TABLE artist_youtube_current (
  artist_id INTEGER PRIMARY KEY REFERENCES artists(id) ON DELETE CASCADE,
  channel_id TEXT NOT NULL,
  subscriber_count INTEGER CHECK (subscriber_count >= 0),   -- NULL when the channel hides it
  view_count INTEGER CHECK (view_count >= 0),
  video_count INTEGER CHECK (video_count >= 0),
  fetched_at TEXT NOT NULL,
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL
);

-- ---- Ticketmaster status history -----------------------------------------------------

-- One row each time ingestion sees an event's status differ from the last one recorded.
-- The change happened somewhere between previous_seen_at and seen_at.
CREATE TABLE event_status_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  previous_status TEXT,                      -- NULL for the first status we saw
  previous_seen_at TEXT,                     -- last time the previous status was seen
  seen_at TEXT NOT NULL,                     -- first time this status was seen
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL
);
CREATE INDEX event_status_history_event ON event_status_history (event_id, seen_at);

-- ---- Derived features ------------------------------------------------------------------

-- Wikipedia pageview momentum. For N in 7, 30, 90: the average daily views over the last N days
-- vs the N days before that (0.5 = up 50%). A window counts only if at least 90% of its days
-- have data. spike = 1 when the 7-day average is more than twice the 90-day average.
CREATE VIEW v_artist_pageview_momentum AS
WITH latest AS (
  SELECT artist_id, MAX(day) AS day FROM artist_pageviews GROUP BY artist_id
),
w AS (
  SELECT l.artist_id, l.day AS latest_day,
    AVG(CASE WHEN p.day > date(l.day, '-7 days') THEN p.views END) AS avg_7,
    COUNT(CASE WHEN p.day > date(l.day, '-7 days') THEN 1 END) AS n_7,
    AVG(CASE WHEN p.day <= date(l.day, '-7 days') AND p.day > date(l.day, '-14 days') THEN p.views END) AS prev_7,
    COUNT(CASE WHEN p.day <= date(l.day, '-7 days') AND p.day > date(l.day, '-14 days') THEN 1 END) AS n_prev_7,
    AVG(CASE WHEN p.day > date(l.day, '-30 days') THEN p.views END) AS avg_30,
    COUNT(CASE WHEN p.day > date(l.day, '-30 days') THEN 1 END) AS n_30,
    AVG(CASE WHEN p.day <= date(l.day, '-30 days') AND p.day > date(l.day, '-60 days') THEN p.views END) AS prev_30,
    COUNT(CASE WHEN p.day <= date(l.day, '-30 days') AND p.day > date(l.day, '-60 days') THEN 1 END) AS n_prev_30,
    AVG(CASE WHEN p.day > date(l.day, '-90 days') THEN p.views END) AS avg_90,
    COUNT(CASE WHEN p.day > date(l.day, '-90 days') THEN 1 END) AS n_90,
    AVG(CASE WHEN p.day <= date(l.day, '-90 days') AND p.day > date(l.day, '-180 days') THEN p.views END) AS prev_90,
    COUNT(CASE WHEN p.day <= date(l.day, '-90 days') AND p.day > date(l.day, '-180 days') THEN 1 END) AS n_prev_90
  FROM latest l JOIN artist_pageviews p ON p.artist_id = l.artist_id
  GROUP BY l.artist_id, l.day
)
SELECT artist_id, latest_day,
  CASE WHEN n_7 >= 7 * 0.9 THEN avg_7 END AS views_avg_7d,
  CASE WHEN n_30 >= 30 * 0.9 THEN avg_30 END AS views_avg_30d,
  CASE WHEN n_90 >= 90 * 0.9 THEN avg_90 END AS views_avg_90d,
  CASE WHEN n_7 >= 7 * 0.9 AND n_prev_7 >= 7 * 0.9 AND prev_7 > 0 THEN avg_7 / prev_7 - 1 END AS views_change_7d,
  CASE WHEN n_30 >= 30 * 0.9 AND n_prev_30 >= 30 * 0.9 AND prev_30 > 0 THEN avg_30 / prev_30 - 1 END AS views_change_30d,
  CASE WHEN n_90 >= 90 * 0.9 AND n_prev_90 >= 90 * 0.9 AND prev_90 > 0 THEN avg_90 / prev_90 - 1 END AS views_change_90d,
  CASE WHEN n_7 >= 7 * 0.9 AND n_90 >= 90 * 0.9 THEN (CASE WHEN avg_7 > 2 * avg_90 THEN 1 ELSE 0 END) END AS views_spike
FROM w;

-- Latest ListenBrainz totals per artist.
CREATE VIEW v_artist_listenbrainz AS
SELECT s.artist_id, s.captured_on, s.listenbrainz_listeners, s.listenbrainz_listens
FROM artist_metrics_snapshots s
WHERE s.listenbrainz_listeners IS NOT NULL
  AND s.captured_on = (SELECT MAX(captured_on) FROM artist_metrics_snapshots x
                       WHERE x.artist_id = s.artist_id AND x.listenbrainz_listeners IS NOT NULL);

-- Listener counts per venue seat, one column per source (Last.fm, ListenBrainz), plus pageviews.
-- YouTube is deliberately absent: its policies forbid metrics derived from its data.
CREATE VIEW v_event_demand_sources AS
SELECT h.event_id, h.artist_id, p.capacity,
  a.lastfm_listeners,
  CASE WHEN p.capacity > 0 AND a.lastfm_listeners IS NOT NULL THEN 1.0 * a.lastfm_listeners / p.capacity END AS lastfm_listeners_per_seat,
  lb.listenbrainz_listeners,
  CASE WHEN p.capacity > 0 AND lb.listenbrainz_listeners IS NOT NULL THEN 1.0 * lb.listenbrainz_listeners / p.capacity END AS listenbrainz_listeners_per_seat,
  pv.views_avg_30d, pv.views_change_7d, pv.views_change_30d, pv.views_change_90d, pv.views_spike
FROM v_event_headliner h
JOIN artists a ON a.id = h.artist_id
JOIN v_event_place p ON p.event_id = h.event_id
LEFT JOIN v_artist_listenbrainz lb ON lb.artist_id = h.artist_id
LEFT JOIN v_artist_pageview_momentum pv ON pv.artist_id = h.artist_id;

-- Sellout-speed PROXY: hours from public on-sale to the first time Ticketmaster showed the event
-- as 'offsale' before the show. Offsale doesn't always mean sold out (holds, venue changes, sales
-- moving elsewhere), so is_proxy is always 1. uncertainty_hours is how long the change could
-- have happened before we saw it (the gap between checks).
CREATE VIEW v_event_sellout_proxy AS
WITH first_offsale AS (
  SELECT event_id, MIN(seen_at) AS seen_at FROM event_status_history WHERE status = 'offsale' GROUP BY event_id
)
SELECT e.id AS event_id, e.venue_id, h.artist_id, e.onsale_at, f.seen_at AS offsale_seen_at,
  ROUND((julianday(f.seen_at) - julianday(e.onsale_at)) * 24, 1) AS hours_onsale_to_offsale,
  ROUND((julianday(f.seen_at) - julianday(sh.previous_seen_at)) * 24, 1) AS uncertainty_hours,
  1 AS is_proxy
FROM first_offsale f
JOIN events e ON e.id = f.event_id
LEFT JOIN v_event_headliner h ON h.event_id = e.id
LEFT JOIN event_status_history sh ON sh.event_id = f.event_id AND sh.seen_at = f.seen_at AND sh.status = 'offsale'
WHERE e.onsale_at IS NOT NULL
  AND julianday(f.seen_at) >= julianday(e.onsale_at)
  AND (e.event_date IS NULL OR f.seen_at < e.event_date)
  AND COALESCE(e.status, 'unknown') NOT IN ('cancelled', 'postponed', 'rescheduled');

CREATE VIEW v_artist_sellout_proxy AS
WITH m AS (
  SELECT artist_id, hours_onsale_to_offsale AS h,
    ROW_NUMBER() OVER (PARTITION BY artist_id ORDER BY hours_onsale_to_offsale) AS rn,
    COUNT(*) OVER (PARTITION BY artist_id) AS n
  FROM v_event_sellout_proxy WHERE artist_id IS NOT NULL
)
SELECT artist_id, n AS events, AVG(h) AS median_hours_to_offsale, 1 AS is_proxy
FROM m WHERE rn IN ((n + 1) / 2, (n + 2) / 2)
GROUP BY artist_id, n;

CREATE VIEW v_venue_sellout_proxy AS
WITH m AS (
  SELECT venue_id, hours_onsale_to_offsale AS h,
    ROW_NUMBER() OVER (PARTITION BY venue_id ORDER BY hours_onsale_to_offsale) AS rn,
    COUNT(*) OVER (PARTITION BY venue_id) AS n
  FROM v_event_sellout_proxy WHERE venue_id IS NOT NULL
)
SELECT venue_id, n AS events, AVG(h) AS median_hours_to_offsale, 1 AS is_proxy
FROM m WHERE rn IN ((n + 1) / 2, (n + 2) / 2)
GROUP BY venue_id, n;

-- The one-row-per-event feature view, now with the new sources.
DROP VIEW v_event_features;
CREATE VIEW v_event_features AS
SELECT e.id AS event_id, e.ticketmaster_id, e.name, e.event_date, e.status, e.venue_id,
  d.artist_id, d.listeners, d.playcount, d.plays_per_listener, d.growth_30d, d.growth_90d,
  d.capacity, d.listeners_per_seat,
  ds.listenbrainz_listeners, ds.listenbrainz_listeners_per_seat,
  ds.views_avg_30d, ds.views_change_7d, ds.views_change_30d, ds.views_change_90d, ds.views_spike,
  sp.hours_onsale_to_offsale, asp.median_hours_to_offsale AS artist_median_hours_to_offsale,
  vsp.median_hours_to_offsale AS venue_median_hours_to_offsale,
  s.tour_dates, s.dates_in_market_on_tour, s.days_since_last_in_market, s.dates_last_365d,
  vm.metro_population, vm.has_price_cap, vm.restricts_transfer,
  pr.face, pr.face_source, pr.face_fee_included, pr.resale, pr.resale_basis, pr.resale_source, pr.resale_as_of,
  gi.get_in, gi.get_in_source, gi.get_in_as_of, gi.get_in_7d_ago, gi.get_in_trend_7d
FROM events e
LEFT JOIN v_event_demand d ON d.event_id = e.id
LEFT JOIN v_event_demand_sources ds ON ds.event_id = e.id
LEFT JOIN v_event_sellout_proxy sp ON sp.event_id = e.id
LEFT JOIN v_artist_sellout_proxy asp ON asp.artist_id = d.artist_id
LEFT JOIN v_venue_sellout_proxy vsp ON vsp.venue_id = e.venue_id
LEFT JOIN v_event_scarcity s ON s.event_id = e.id
LEFT JOIN venue_market vm ON vm.venue_id = e.venue_id
LEFT JOIN v_event_prices pr ON pr.event_id = e.id
LEFT JOIN v_event_get_in gi ON gi.event_id = e.id;
