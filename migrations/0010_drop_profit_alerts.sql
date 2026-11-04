-- Alerts are for followed artists only, so the two other alert settings are dropped.
ALTER TABLE preferences DROP COLUMN profit_alerts;
ALTER TABLE preferences DROP COLUMN profit_threshold;
