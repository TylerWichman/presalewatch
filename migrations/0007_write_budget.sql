-- Fewer rows written: D1's free tier allows 100,000 a day for the whole database, index entries
-- included, and the daily Wikipedia pageview rows alone used ~87,000 on 3 October 2026.
-- See docs/database.md (Write budget).

-- ---- Pageviews: weekly totals instead of daily rows -----------------------------------
-- One row per artist per Monday-to-Sunday week, about 1/7 the rows. WITHOUT ROWID makes the
-- primary key the table itself, so each row is one write instead of two (row + key index).
CREATE TABLE artist_pageviews_weekly (
  artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
  week_start TEXT NOT NULL,                  -- the Monday, 'YYYY-MM-DD'
  views INTEGER NOT NULL CHECK (views >= 0), -- human views over the days with data
  days INTEGER NOT NULL CHECK (days BETWEEN 1 AND 7),  -- days with data (7 for a full week)
  article TEXT NOT NULL,                     -- the title the views were counted for
  source TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  PRIMARY KEY (artist_id, week_start)
) WITHOUT ROWID;

-- Carry over the daily history, except each artist's latest week if it isn't over yet.
INSERT INTO artist_pageviews_weekly (artist_id, week_start, views, days, article, source, last_updated)
SELECT d.artist_id, d.week_start, SUM(d.views), COUNT(*), MAX(d.article), MAX(d.source), MAX(d.last_updated)
FROM (
  SELECT artist_id, views, article, source, last_updated, day,
    date(day, '-' || ((CAST(strftime('%w', day) AS INTEGER) + 6) % 7) || ' days') AS week_start
  FROM artist_pageviews
) d
JOIN (SELECT artist_id, MAX(day) AS last_day FROM artist_pageviews GROUP BY artist_id) l ON l.artist_id = d.artist_id
WHERE date(d.week_start, '+6 days') <= l.last_day
GROUP BY d.artist_id, d.week_start;

DROP VIEW v_artist_pageview_momentum;
DROP TABLE artist_pageviews;

-- Same columns as before, from weekly totals: the last 1, 4, and 13 complete weeks (7, 28, and
-- 91 days) vs the same number of weeks before. Averages are per day with data. A window counts
-- only if at least 90% of its days have data. spike = 1 when last week's daily average is more
-- than twice the 13-week average.
CREATE VIEW v_artist_pageview_momentum AS
WITH latest AS (
  SELECT artist_id, MAX(week_start) AS week FROM artist_pageviews_weekly GROUP BY artist_id
),
w AS (
  SELECT l.artist_id, date(l.week, '+6 days') AS latest_day,
    SUM(CASE WHEN p.week_start > date(l.week, '-7 days') THEN p.views END) AS v_1,
    SUM(CASE WHEN p.week_start > date(l.week, '-7 days') THEN p.days END) AS n_1,
    SUM(CASE WHEN p.week_start <= date(l.week, '-7 days') AND p.week_start > date(l.week, '-14 days') THEN p.views END) AS pv_1,
    SUM(CASE WHEN p.week_start <= date(l.week, '-7 days') AND p.week_start > date(l.week, '-14 days') THEN p.days END) AS pn_1,
    SUM(CASE WHEN p.week_start > date(l.week, '-28 days') THEN p.views END) AS v_4,
    SUM(CASE WHEN p.week_start > date(l.week, '-28 days') THEN p.days END) AS n_4,
    SUM(CASE WHEN p.week_start <= date(l.week, '-28 days') AND p.week_start > date(l.week, '-56 days') THEN p.views END) AS pv_4,
    SUM(CASE WHEN p.week_start <= date(l.week, '-28 days') AND p.week_start > date(l.week, '-56 days') THEN p.days END) AS pn_4,
    SUM(CASE WHEN p.week_start > date(l.week, '-91 days') THEN p.views END) AS v_13,
    SUM(CASE WHEN p.week_start > date(l.week, '-91 days') THEN p.days END) AS n_13,
    SUM(CASE WHEN p.week_start <= date(l.week, '-91 days') AND p.week_start > date(l.week, '-182 days') THEN p.views END) AS pv_13,
    SUM(CASE WHEN p.week_start <= date(l.week, '-91 days') AND p.week_start > date(l.week, '-182 days') THEN p.days END) AS pn_13
  FROM latest l JOIN artist_pageviews_weekly p ON p.artist_id = l.artist_id
  GROUP BY l.artist_id, l.week
)
SELECT artist_id, latest_day,
  CASE WHEN n_1 >= 7 * 0.9 THEN 1.0 * v_1 / n_1 END AS views_avg_7d,
  CASE WHEN n_4 >= 28 * 0.9 THEN 1.0 * v_4 / n_4 END AS views_avg_30d,
  CASE WHEN n_13 >= 91 * 0.9 THEN 1.0 * v_13 / n_13 END AS views_avg_90d,
  CASE WHEN n_1 >= 7 * 0.9 AND pn_1 >= 7 * 0.9 AND pv_1 > 0 THEN (1.0 * v_1 / n_1) / (1.0 * pv_1 / pn_1) - 1 END AS views_change_7d,
  CASE WHEN n_4 >= 28 * 0.9 AND pn_4 >= 28 * 0.9 AND pv_4 > 0 THEN (1.0 * v_4 / n_4) / (1.0 * pv_4 / pn_4) - 1 END AS views_change_30d,
  CASE WHEN n_13 >= 91 * 0.9 AND pn_13 >= 91 * 0.9 AND pv_13 > 0 THEN (1.0 * v_13 / n_13) / (1.0 * pv_13 / pn_13) - 1 END AS views_change_90d,
  CASE WHEN n_1 >= 7 * 0.9 AND n_13 >= 91 * 0.9 THEN (CASE WHEN 1.0 * v_1 / n_1 > 2.0 * v_13 / n_13 THEN 1 ELSE 0 END) END AS views_spike
FROM w;

-- ---- Indexes no query uses ------------------------------------------------------------
-- Every index adds a write to each insert (and to updates of its columns). These are used by no
-- query in the jobs, the pipeline, the views, the accounts API, or the alert Worker
-- (checked with EXPLAIN QUERY PLAN), or duplicate a UNIQUE constraint's index.
DROP INDEX artists_name_key;                   -- unused: artists are matched by external IDs
DROP INDEX venues_name_key;                    -- unused
DROP INDEX venues_metro;                       -- unused (metros is empty)
DROP INDEX artist_aliases_key;                 -- unused
DROP INDEX presales_window;                    -- unused
DROP INDEX artist_metrics_by_date;             -- duplicates UNIQUE (artist_id, captured_on, source)
DROP INDEX venue_capacity_observations_venue;  -- duplicates UNIQUE (venue_id, source, source_id)
