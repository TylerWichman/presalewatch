-- A MusicBrainz ID found to belong to someone else (for example Ticketmaster linking the singer
-- Engelbert Humperdinck to the 19th-century composer). Kept so no job attaches it again.
-- See docs/database.md (Artists).

ALTER TABLE artists ADD COLUMN mbid_rejected TEXT;
