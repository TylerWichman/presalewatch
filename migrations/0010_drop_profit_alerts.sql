-- The High profit alert is gone (PouchIt now just lists presales), so its two settings are dropped.
ALTER TABLE preferences DROP COLUMN profit_alerts;
ALTER TABLE preferences DROP COLUMN profit_threshold;
