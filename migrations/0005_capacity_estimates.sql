-- Estimated capacity for venues no source can resolve. Kept apart from venues.capacity, which only
-- ever holds a measured value, so an estimate can never be mistaken for a measurement.
-- See docs/database.md (Venue enrichment > Estimated capacity).

ALTER TABLE venues ADD COLUMN capacity_estimate INTEGER CHECK (capacity_estimate > 0);
ALTER TABLE venues ADD COLUMN capacity_estimate_basis TEXT;   -- e.g. '25th percentile of 97 measured venues <= 3,000'
ALTER TABLE venues ADD COLUMN capacity_estimate_at TEXT;
