# PresaleWatch database

The artist, venue, and event database behind PresaleWatch's demand and resale scoring.
It lives in Cloudflare D1 (SQLite) next to the accounts tables. The schema is in
[`migrations/0002_intel.sql`](../migrations/0002_intel.sql), and tests are in
[`tests/test_schema.py`](../tests/test_schema.py).

## How data gets in

- Ingestion jobs run in GitHub Actions (Python) and write to D1 through Cloudflare's
  D1 HTTP API in batches. Workers stay limited to accounts and alerts.
- Each source has its own job: Ticketmaster, Last.fm, MusicBrainz, and capacity drafts.
  Every job logs itself in `ingest_runs`.
- The page and alerts keep reading `presales.json`, which is exported from D1.

## Conventions

- **Times:** ISO-8601 UTC text, like `2026-10-02T12:34:57Z`. Event dates are the
  venue's local date, `YYYY-MM-DD`.
- **Bookkeeping:** every table records `source` (which API or person the row came
  from) and `last_updated`.
- **Missing data is NULL.** Nothing is estimated or filled in at this layer. If a
  number isn't known, it's empty.
- **Upserts:** external IDs (Ticketmaster, SeatGeek, MusicBrainz, Wikidata) are unique,
  so re-running a job updates rows instead of duplicating them.
- **Events are never deleted.** Past dates stay, so tour history builds up over time.
- **Money math happens in one place.** Fees, the ask-to-sale discount, and the Profit %
  formula live in `config/model.json` and `edge.py`. The views return raw prices and ratios.

## Tables

### Reference tables (kept by hand)

**`metros`**: metropolitan areas, so shows in nearby cities count as one market.

| Field | Meaning |
| --- | --- |
| `id` | Internal ID |
| `name` | Metro name, e.g. "New York-Newark-Jersey City" |
| `cbsa_code` | US Census metro (CBSA) code |
| `population` | Metro population |
| `population_year` | Year of that population figure |
| `ticketmaster_market_ids` | Ticketmaster market IDs in this metro, `;`-separated |

**`state_resale_rules`**: state laws that affect reselling, one row per state.

| Field | Meaning |
| --- | --- |
| `state` | Two-letter code |
| `has_price_cap` | 1 if resale prices are capped, 0 if not, NULL if not yet researched |
| `price_cap_note` | Plain-English summary of the cap |
| `restricts_transfer` | 1 if sellers may make tickets non-transferable |
| `transfer_note` | Plain-English summary |
| `law_reference` | Statute or source link |
| `reviewed_on` | Date a person last checked the row |

### Artists

**`artists`**

| Field | Meaning |
| --- | --- |
| `id` | Internal ID |
| `name`, `name_key` | Display name, and the normalized form used for matching |
| `mbid` | MusicBrainz artist ID: the main key for matching artists across sources |
| `ticketmaster_id` | Ticketmaster attraction ID |
| `seatgeek_id` | SeatGeek performer ID (empty until partner access) |
| `lastfm_name` | The name Last.fm uses |
| `lastfm_listeners`, `lastfm_playcount` | Latest Last.fm counts (history is in `artist_metrics_snapshots`) |
| `lastfm_checked_at`, `musicbrainz_checked_at` | When each source was last asked |
| `active_from`, `active_to` | Years active (MusicBrainz); `active_to` is NULL if still active or unknown |

**`artist_tags`**: genre and style tags, such as Last.fm's "indie rock". The key is
`(artist_id, tag, source)`; `rank` 1 is the most-applied tag.

**`artist_similar`**: similar artists. `match` is Last.fm's 0-1 similarity.
`similar_artist_id` links to our own row when we track that artist too.

**`artist_metrics_snapshots`**: one row per artist, per day, per source.

| Field | Meaning |
| --- | --- |
| `captured_on` | Date of the reading |
| `lastfm_listeners`, `lastfm_playcount` | Counts that day |
| `seatgeek_score`, `seatgeek_popularity` | Empty until SeatGeek partner access |

### Venues

**`venues`**

| Field | Meaning |
| --- | --- |
| `id`, `name`, `name_key` | Internal ID, display name, matching key |
| `ticketmaster_id`, `seatgeek_id`, `wikidata_id` | External IDs |
| `address`, `city`, `state`, `postal_code`, `country` | Location |
| `latitude`, `longitude` | From Ticketmaster; used to match venues across sources |
| `metro_id` | The venue's metro (links to `metros`) |
| `capacity` | Concert capacity |
| `capacity_source`, `capacity_source_url`, `capacity_note` | Where the capacity came from, e.g. "wikipedia" plus the page link, and notes like "6,500 for basketball" |
| `capacity_verified` | 1 once a person has checked the capacity |
| `venue_type` | `club`, `theater`, `amphitheater`, `arena`, `stadium`, `festival`, or `other` |
| `primary_platform` | Main ticketing platform, e.g. `ticketmaster` |

### Events and presales

**`events`**

| Field | Meaning |
| --- | --- |
| `id` | Internal ID |
| `ticketmaster_id`, `seatgeek_id` | External IDs |
| `name`, `tour_name` | Event title and tour name |
| `venue_id` | Where it is |
| `event_date`, `event_time`, `starts_at` | Local date and time, and UTC start when known |
| `status` | `onsale`, `offsale`, `cancelled`, `postponed`, `rescheduled`, or `unknown` |
| `sold_out` | 1 or 0 only when a source says so; NULL means unknown |
| `face_min`, `face_max`, `face_currency` | Standard ticket price range |
| `face_fee_included` | 1 if the face price already includes fees |
| `dynamic_pricing` | 1 if Ticketmaster flags dynamic pricing |
| `onsale_at` | Public on-sale start (UTC) |
| `ticket_limit` | Per-buyer ticket limit, if published |
| `first_seen_at`, `last_seen_at` | When a source first and last listed the event |

**`event_artists`**: who plays each event. `position` 0 is the headliner, then billing order.

**`presales`**

| Field | Meaning |
| --- | --- |
| `event_id` | The event |
| `name`, `presale_type` | Presale name and type: Artist, Amex, Citi, Venue, Live Nation, Spotify, VIP, ... |
| `starts_at`, `ends_at` | Presale window (UTC) |
| `access_requirements` | How to get access, e.g. "Citi card, no code" |
| `description`, `url`, `link_text` | Details and sign-up link |

### Prices

**`resale_snapshots`**: resale price readings from APIs over time. You can only add
rows: editing is blocked, and deleting is allowed only so a source's data-retention
rules can be followed.

| Field | Meaning |
| --- | --- |
| `captured_at` | When the reading was taken |
| `lowest`, `median`, `average` | Prices per ticket |
| `listing_count` | How many listings |
| `price_basis` | `ask` (listing prices) or `sold` (completed sales) |

**`observed_prices`**: prices logged by hand and imported from CSV. They're used
whenever API data is missing.

| Field | Meaning |
| --- | --- |
| `event_ref` | What the CSV gave to identify the event (Ticketmaster event ID or URL) |
| `event_id` | The matched event; NULL until matched (unmatched rows go to `match_review`) |
| `kind` | `face` (original price) or `resale` |
| `price_basis` | For resale: `ask` (a listing) or `sold` (a sale). Required for resale |
| `section_tier` | e.g. "GA floor", "Sec 104", "Platinum" |
| `standard_ticket` | 0 for VIP or Platinum; only standard tickets count toward medians |
| `price_point` | `single` (one ticket's listing or sale price), `get_in` (the cheapest listing for the whole event at that moment), or `median` (a marketplace's own median). Get-in rows must be resale listing prices, and they're kept out of medians |
| `price` | Price per ticket |
| `fees_included` | 1 if the price includes fees |
| `observed_on` | Date the price was seen |
| `source` | Where it was seen, e.g. "StubHub listing" |
| `import_batch` | Which CSV import added the row |

### Matching and bookkeeping

**`match_review`**: anything a person needs to look at. One table covers artists and
venues that didn't match, capacity drafts, and imported prices with no matching event.
`kind` says which. `details` holds the evidence as JSON, `candidate_id` and
`candidate_score` hold the best guess, and `status` is `open`, `accepted`, or `rejected`.

**`ingest_runs`**: one row per ingestion run, with its source, start and finish times,
status, API calls, and rows written.

## Derived features (views)

| View | What it gives, per row |
| --- | --- |
| `v_event_headliner` | Each event's headliner |
| `v_event_place` | Each event's market: its metro when known, else its city. Also date, status, venue type, and capacity |
| `v_artist_momentum` | Latest Last.fm listeners, plus the reading from 30-60 days and 90-120 days earlier. Empty until enough history exists |
| `v_event_demand` | Headliner listeners, plays per listener, 30- and 90-day listener growth, venue capacity, and **listeners per seat** |
| `tour_history` | Every dated, non-cancelled date seen for each headliner |
| `v_event_scarcity` | `tour_dates` (the artist's dates within ±90 days), `dates_in_market_on_tour` (how many of those are in this market), `days_since_last_in_market` (days back to the artist's last date here before this tour), `dates_last_365d` (tour frequency) |
| `v_observed_medians` | Median hand-logged price per event and kind (standard tickets only) |
| `v_event_prices` | One face price and one resale price per event. API data comes first; hand-logged prices fill gaps, preferring sales over asks. The view also says where each price came from |
| `v_event_markup` | Resale ÷ face, where both exist |
| `v_event_get_in` | **Get-in price**: the cheapest listing for the event, now and about a week ago, plus the 7-day trend (0.25 means up 25%). Readings come from API snapshots when an event has any, otherwise from hand-logged get-in prices. The "week ago" reading is the latest one 5-9 days back, so logging every few days is enough. In scoring, get-in gives a **floor Profit %**: what you'd make selling at today's cheapest listing |
| `v_venue_premium`, `v_venue_type_premium` | Median markup per venue and per venue type, kept separate for asks and sales |
| `v_artist_premium` | Median markup per headliner, plus sellout rate over events where sellout is known |
| `venue_market` | Each venue's metro population and its state's resale rules |
| `v_event_features` | Everything above in one row per event. Scoring reads this |

**Expected margin is not a view.** It uses the existing Profit % formula in `edge.py`:
(resale × (1 − seller fee) − all-in cost) ÷ all-in cost, where the all-in cost is face plus
primary fees. The fee rates and the ask-to-sale discount come from `config/model.json`.
Profit % never adjusts for the odds of getting tickets.

## What stays empty for now, and why

| Data | Why | What fills it |
| --- | --- | --- |
| `resale_snapshots` | No free resale API returns prices (SeatGeek's free tier returns an empty `stats` object) | SeatGeek partner access, or `observed_prices` in the meantime |
| Venue and artist premiums, sellout rate | Need resale prices and sellout reports | Same as above |
| `face_min`/`face_max` on most events | Ticketmaster rarely publishes prices before on-sale | Ticketmaster after on-sale, or `observed_prices` |
| `seatgeek_*` columns | Left out until partner access | SeatGeek partner access |
| Momentum | Needs 30-120 days of daily snapshots | Builds up after the Last.fm job starts |
| `days_since_last_in_market`, `dates_last_365d` | History starts when ingestion starts | Builds up over time |
| `metros`, `state_resale_rules` | Hand-maintained | Entered by hand |
