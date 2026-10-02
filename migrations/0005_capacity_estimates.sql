-- Estimated capacity for venues no source can resolve. Kept apart from venues.capacity, which only
-- ever holds a measured value, so an estimate can never be mistaken for a measurement.
-- See docs/database.md (Venue enrichment > Estimated capacity).

ALTER TABLE venues ADD COLUMN capacity_estimate INTEGER CHECK (capacity_estimate > 0);
ALTER TABLE venues ADD COLUMN capacity_estimate_basis TEXT;   -- e.g. '25th percentile of 97 measured venues <= 3,000'
ALTER TABLE venues ADD COLUMN capacity_estimate_at TEXT;

-- OpenStreetMap runs as its own capped daily step after Wikidata and Wikipedia; this records when
-- a venue was last checked against it (an estimate needs every source to have been tried).
ALTER TABLE venues ADD COLUMN osm_checked_at TEXT;

-- Market size: everyone living within 80 km of the venue (2020 Census tract centers of population).
-- Replaces the hand-set top-market list. See ingest/catchment.py.
ALTER TABLE venues ADD COLUMN catchment_population INTEGER CHECK (catchment_population >= 0);
ALTER TABLE venues ADD COLUMN catchment_basis TEXT;          -- source and radius, e.g. '2020 Census tract centers of population, within 80 km'
ALTER TABLE venues ADD COLUMN catchment_at TEXT;
