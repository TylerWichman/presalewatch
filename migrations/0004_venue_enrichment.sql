-- Self-filling venue enrichment: capacity with its raw text, source, and confidence, plus venue
-- details, from Wikidata, Wikipedia, OpenStreetMap, Ticketmaster, and hand-entered values.
-- See docs/database.md.

ALTER TABLE venues ADD COLUMN capacity_raw TEXT;            -- the text the capacity was parsed from
ALTER TABLE venues ADD COLUMN capacity_confidence TEXT CHECK (capacity_confidence IN ('high', 'medium', 'low'));
ALTER TABLE venues ADD COLUMN capacity_checked_at TEXT;     -- last time the capacity sources were consulted
ALTER TABLE venues ADD COLUMN opened TEXT;                  -- opening date: 'YYYY' or 'YYYY-MM-DD'
ALTER TABLE venues ADD COLUMN operator TEXT;
ALTER TABLE venues ADD COLUMN wikipedia_title TEXT;
ALTER TABLE venues ADD COLUMN osm_id TEXT;                  -- 'node/1', 'way/2', or 'relation/3'
ALTER TABLE venues ADD COLUMN timezone TEXT;                -- from Ticketmaster, e.g. 'America/New_York'
ALTER TABLE venues ADD COLUMN url TEXT;                     -- Ticketmaster venue page
ALTER TABLE venues ADD COLUMN ticketmaster_checked_at TEXT;

-- Every capacity any source reported for a venue, kept for audit and re-verification.
-- venues.capacity holds the one in use; this table shows where it (and any disagreement) came from.
CREATE TABLE venue_capacity_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK (source IN ('manual', 'wikidata', 'wikipedia', 'openstreetmap')),
  source_id TEXT NOT NULL,                   -- Wikidata QID, article title, OSM element, or 'config'/'csv'
  source_url TEXT,
  raw_text TEXT,                             -- exactly what the source said
  parsed_capacity INTEGER CHECK (parsed_capacity > 0),
  label TEXT,                                -- the configuration it applies to, e.g. 'concerts', when stated
  confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
  match_distance_m REAL,                     -- distance between Ticketmaster's and the source's coordinates
  observed_at TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  UNIQUE (venue_id, source, source_id)
);
CREATE INDEX venue_capacity_observations_venue ON venue_capacity_observations (venue_id);
