# PouchIt database

The artist, venue, and event database behind PouchIt's demand and resale scoring.
It lives in Cloudflare D1 (SQLite) next to the accounts tables. The schema is in
[`migrations/0002_intel.sql`](../migrations/0002_intel.sql) and
[`migrations/0003_demand_sources.sql`](../migrations/0003_demand_sources.sql). Tests are in
[`tests/test_schema.py`](../tests/test_schema.py) and [`tests/test_ingest.py`](../tests/test_ingest.py).

## How data gets in

- **Where it runs:** ingestion jobs in [`ingest/`](../ingest) run daily in GitHub Actions
  ([`.github/workflows/ingest.yml`](../.github/workflows/ingest.yml)). They write to D1 through
  Cloudflare's D1 HTTP API in batches. Workers stay limited to accounts and alerts.
- **One job per source, in this order:** Ticketmaster → Last.fm → MusicBrainz → Wikidata →
  venue enrichment → capacity estimates → catchment population → Wikipedia pageviews → ListenBrainz
  → YouTube. Each logs itself in `ingest_runs`, and one
  failing job doesn't stop the others.
- **Effect on the live page:** none. The page and alerts still read `presales.json` from
  the existing pipeline, so scoring doesn't change until deliverable 4.
- **Running it yourself:**

  ```powershell
  python -m ingest.run --db sqlite:local.db              # every source, into a local file
  python -m ingest.run --db d1 --sources lastfm          # one source, into production
  python -m ingest.coverage --db d1                      # the coverage report
  python -m ingest.import_prices prices.csv --db d1      # hand-logged prices
  ```

  Writing to D1 needs `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_D1_TOKEN` (a token with only
  D1:Edit; the workflow uses `CLOUDFLARE_BACKEND_TOKEN`).

### How artists and venues are matched

The rule everywhere: accept a match only on strong evidence; anything weaker goes to
`match_review` for a person. Nothing is guessed.

- **MusicBrainz ID (the main artist key)**, in order of trust:
  1. Ticketmaster's own MusicBrainz link for the artist.
  2. The ID Last.fm returns, if Last.fm's artist name matches ours.
  3. A MusicBrainz search that returns exactly one artist with the same name (or alias) and a
     full score. Several artists sharing a name ("Low", "Bush") always go to review.

  An ID already held by another artist row is never reassigned; that goes to review too.
- **Wikidata item:** from the artist's MusicBrainz page, or a Wikidata lookup by MusicBrainz ID
  (property P434). An item is only kept if it carries our MusicBrainz ID. The Wikipedia title
  and YouTube channel come from that item.
- **Venues:** see [Venue enrichment](#venue-enrichment) below.

### Source terms on storing data

Checked October 2026, before anything was stored.

| Source | License or terms | Retention limits | What we do |
| --- | --- | --- | --- |
| Ticketmaster Discovery | May store event content "for reasonable periods" to provide the service. May not derive revenue from the API | Vague: "reasonable periods" | Store only the facts scoring and tour history need. **A paid PouchIt needs Ticketmaster's OK** |
| Last.fm | **Non-commercial use only** (ToS 3.1). Credit Last.fm. Stored Last.fm data capped at 100 MB (4.3.4) | 100 MB total | Well under the cap. **A paid PouchIt needs a commercial license** |
| MusicBrainz | Core data (artists, aliases, relationships) is CC0. 1 request/second per IP, contactable User-Agent | None | Core data only |
| Wikidata | CC0 | None | Contactable User-Agent, one request at a time |
| Wikipedia pageviews | CC0. User-Agent with contact details required | None | 12-month backfill, then daily |
| ListenBrainz | CC0, commercial use allowed (MetaBrainz asks commercial users to donate) | None | Totals only; per-user listener names are dropped |
| Wikipedia (MediaWiki API) | Article text is CC BY-SA 4.0. A capacity number is a fact, and the article is stored as its source link. Sequential requests, descriptive User-Agent, `maxlag` | None | Store the parsed number, the raw infobox text, and the article link. Credit and link Wikipedia wherever a value is shown |
| OpenStreetMap (Overpass API) | **ODbL**. Credit "© OpenStreetMap contributors". Using OSM data publicly in a database derived from it means offering the OSM-derived part under ODbL on request. Overpass fair use: about 10,000 requests and 1 GB a day | None | Last-resort source. Every value it supplies is marked `capacity_source = 'openstreetmap'`, so it can be shared or removed on its own. **Add the OSM credit to the site before using these values publicly** |
| YouTube Data API | Developer Policies III.E.4 | **30 days** for public channel statistics (III.E.4.d). **No derived metrics** (III.E.4.h) | Current values only, one row per artist, deleted after 30 days. Not combined with other data and not used in scoring |

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

**`artist_metrics_snapshots`**: one row per artist, per day, per source. The daily Last.fm job
refreshes each artist every 7 days (`refresh.lastfm_ttl_hours`) and stores listeners and playcount
with each refresh, so an artist gets a reading about weekly.

**Recency signal to test (not in the score yet):** Last.fm plays gained over the last 30 days, per venue
seat. Test it once 30 days of snapshots exist (first snapshots: 2 October 2026), and only after resale
price data exists to check it against. Wikipedia pageviews per seat were tried for this and rejected:
they track who reads Wikipedia (older, established acts) more than current demand.

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

### Demand sources (migration 0003)

**New columns on `artists`**

| Field | Meaning |
| --- | --- |
| `wikidata_id` | The artist's Wikidata item, e.g. `Q44190` |
| `wikipedia_title` | English Wikipedia article title |
| `youtube_channel_id` | Official YouTube channel, from Wikidata (P2397) |
| `artist_type` | MusicBrainz type: Person, Group, Orchestra, Choir, Character, Other |
| `country` | MusicBrainz country code |
| `mbid_source` | How the MusicBrainz ID was found: `ticketmaster`, `lastfm`, `musicbrainz_search`, or `manual` |
| `mbid_rejected` | A MusicBrainz ID found to belong to a namesake from another era (a person born before 1900, or an act that ended before 1970), so it's never attached again. Set by the MusicBrainz job, which also clears the Wikidata, Wikipedia, YouTube, pageview, and ListenBrainz data found through that ID and opens a review item. Migration 0006 |
| `wikidata_checked_at`, `listenbrainz_checked_at`, `pageviews_checked_at` | When each source was last asked |

**`artist_aliases`**: other names an artist goes by (from MusicBrainz), with a normalized
`alias_key` for matching.

**New columns on `artist_metrics_snapshots`**: `listenbrainz_listeners` (people who've listened)
and `listenbrainz_listens` (total plays), on rows whose `source` is `listenbrainz`.

**`artist_pageviews`**: daily human views of the artist's English Wikipedia article (`day`,
`views`, and the `article` title they were counted for). It's backfilled 12 months, then
updated daily.

**`artist_youtube_current`**: subscriber, view, and video counts for the artist's channel.
**Current values only**: one row per artist, replaced on each refresh and deleted after 30
days, as YouTube's policies require. `subscriber_count` is NULL when a channel hides it.

**New column on `venues`**: `wikidata_checked_at`.

### Venue enrichment (migration 0004)

Runs daily as part of the ingestion workflow ([`ingest/venue_enrichment.py`](../ingest/venue_enrichment.py)).

**What gets processed each run:** venues with upcoming events, busiest first.
1. Never-checked venues, so new ones are done on the run they first appear.
2. Venues still without a capacity, retried every **30 days**.
3. Filled venues, re-verified every **180 days**.

At most 200 venues a run. Verified (hand-entered) venues are never re-processed.

**Sources, in order.** The first confident match with a usable capacity wins:

| Order | Source | What it gives |
| --- | --- | --- |
| 0 | Hand-entered values: `config/venues.csv` and imported hand-fill CSVs | Capacity. Applied first, marked verified, never overwritten |
| 1 | Wikidata | Capacity (with the configuration it applies to, e.g. concerts), venue type, opening date, operator, Wikipedia article |
| 2 | Wikipedia | The article's infobox `capacity`. Found from Wikidata's link, or by searching for articles near the coordinates |
| 3 | OpenStreetMap | The `capacity` tag on a nearby venue (amenity, leisure, or building tags) |
| – | Ticketmaster venue details | Time zone and venue page, fetched once. Ticketmaster has no capacity field |

**Matching works from coordinates.** For each venue, entries within about 1 km are fetched, then:

| Verdict | Needs |
| --- | --- |
| **Confident** | Within **300 m** of Ticketmaster's coordinates, **and** the same name (or alias, or 60%+ of the same words), **and** a venue-like type (theater, arena, stadium, club, concert hall, ...) |
| **Review** | Within 300 m with only one of name or type, or 300 m to 1 km with a matching name |
| **No** | Anything else |

When two entries describe the same building, the tie-breakers are, in order: the only one carrying a capacity, the only one with a Wikipedia article, then one at least twice as close as the next. Still tied goes to review.

**Capacity parsing** handles messy values like "20,000 (concerts)", "Basketball: 19,722 / Concerts: 20,000", or "Seated: 2,195 / Standing: 3,000":
- **Which value:** the one labeled for concerts. Otherwise the largest.
- **High confidence:** one clear value, or exactly one concert value.
- **Medium confidence:** the largest of several values, or a value marked approximate.
- **Low confidence:** a range ("1,500–2,000") or an implausible number (under 20 or over 150,000). **Low never writes**; it goes to `match_review`.

**Writing rules:**
- Only confident matches with high or medium confidence write a capacity.
- A re-check that finds a number more than 10% different from the stored one goes to review instead of overwriting.
- Details (venue type, opening date, operator, Wikipedia title, OSM ID) only fill empty fields.

**New columns on `venues`**

| Field | Meaning |
| --- | --- |
| `capacity_raw` | The exact text the capacity was parsed from |
| `capacity_confidence` | `high`, `medium`, or `low` |
| `capacity_checked_at` | When the capacity sources were last consulted |
| `opened`, `operator` | Opening date (`YYYY` or `YYYY-MM-DD`) and operator |
| `wikipedia_title`, `osm_id` | The matched Wikipedia article and OpenStreetMap element |
| `timezone`, `url` | From Ticketmaster's venue details |
| `ticketmaster_checked_at` | When those details were fetched |

`capacity_source` is now one of `manual`, `wikidata`, `wikipedia`, or `openstreetmap`.

**`venue_capacity_observations`**: every capacity any source reported for a venue, with the source, its link, the raw text, the parsed number, the configuration label, the confidence, and the distance between the two sets of coordinates. `venues.capacity` holds the one in use; this table shows where it came from and any disagreement.

**Hand-fill fallback.** Venues nothing resolves keep `capacity` NULL. Each workflow run attaches `venues_to_fill.csv`: venues pinned in `capacity_estimate.hand_fill_venues` first, then the rest sorted by the biggest Last.fm audience among their upcoming artists, with a map link, a Wikipedia search link, and any review note. Fill in `capacity` (and ideally `source_url`), then:

```powershell
python -m ingest.venue_handfill import venues_to_fill.csv --db d1
```

Imported values are marked verified and are never overwritten.

**Renamed stadiums and arenas.** Naming rights change often, and Wikidata can lag behind (Daikin Park
used to be Minute Maid Park). A Wikidata entry typed as a stadium or arena within 300 m is a confident
match without a name match, since two stadiums are never that close. It applies only when
Ticketmaster's own venue name also sounds large (stadium, arena, field, park, center, ...), so a room
inside an arena, like The Theater at MSG, can't take the arena's capacity.

### Estimated capacity (migration 0005)

For small venues no source covers, `ingest/venue_estimates.py` (daily, after enrichment) can store an
**estimate** in `venues.capacity_estimate`. It never goes in `venues.capacity`, which only holds
measured values.

| Field | Meaning |
| --- | --- |
| `capacity_estimate` | The estimated capacity |
| `capacity_estimate_basis` | How it was made, e.g. "25th percentile of 97 measured venues <= 3,000" |
| `capacity_estimate_at` | When it was set |

- **Minimum sample:** estimates apply only when at least **100** measured venues of 3,000 seats or fewer
  back the percentile (`capacity_estimate.min_measured`). Below that, estimates are off for the run and
  any existing ones are cleared: on the first production run, 51 venues gave 600 seats, against 850
  from 125.
- **The value** is the **25th percentile** of measured venues of 3,000 seats or fewer. The 25th
  rather than the median, because measured small venues skew large: a club with a Wikipedia page is
  usually a notable, bigger one.
- **Who gets one:** only venues that every source has tried, with nothing open in `match_review`, and
  with no large-venue name or type (stadium, arena, field, park, amphitheater, center, casino, resort,
  hotel, ...). Without that filter, unprocessed or renamed stadiums would get a small-room estimate and
  look like sellouts. Venues listed in `capacity_estimate.hand_fill_venues` (resort rooms whose names
  don't say so, such as The Cosmopolitan of Las Vegas) never get one and head the hand-fill list.
- **In scoring,** an estimate counts for less: it can make an event High only if the event would still
  be High at 3,000 seats. The page labels that rating "High demand · est."
- **Switch:** `capacity_estimate.enabled` in `config/model.json` (on). Turning it off clears every
  estimate on the next run.

### Catchment population (migration 0005)

The market signal in the demand score is the number of people living within 80 km of the venue.
`ingest/catchment.py` (daily) sums the 2020 Census tract populations whose population-weighted center
is within that radius. It replaces the old hand-set top-market list, which counted casinos two hours
from New York as New York and every other city as the same.

| Field | Meaning |
| --- | --- |
| `catchment_population` | People living within the radius |
| `catchment_basis` | Source and radius, e.g. "2020 Census tract centers of population, within 80 km" |
| `catchment_at` | When it was computed |

- **Source:** the Census Bureau's tract centers-of-population file (public domain), downloaded once per
  run. It only changes each decennial census.
- **Recomputed** only for venues without a value or whose basis changed (a new radius in
  `market_population.radius_km`).
- **In scoring:** log scale from 250,000 people (0) to 20 million (1), set in
  `config/model.json` `market_population`. Weight 0.15.

**`event_status_history`**: one row each time Ticketmaster's status for an event changes
(`onsale`, `offsale`, `cancelled`, `postponed`, `rescheduled`). The change happened between
`previous_seen_at` and `seen_at`. Ingestion re-checks events for 30 days after on-sale, since
that's when sellouts happen.

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
| `v_artist_pageview_momentum` | Average daily Wikipedia views over the last 7, 30, and 90 days, and the change vs the period before each (0.5 = up 50%). A window counts only if 90% of its days have data. `views_spike` = 1 when the 7-day average is more than twice the 90-day average |
| `v_artist_listenbrainz` | Latest ListenBrainz listener and listen totals per artist |
| `v_event_demand_sources` | Listeners per venue seat **for each source as its own column** (Last.fm, ListenBrainz), plus the pageview momentum. YouTube is deliberately left out: its policies forbid metrics derived from its data |
| `v_event_sellout_proxy` | **Proxy, not a sellout record.** Hours from public on-sale to the first time Ticketmaster showed the event as off sale before the show. Off sale doesn't always mean sold out (held tickets, sales moving elsewhere), so `is_proxy` is always 1. `uncertainty_hours` is how long the change could have gone unseen between checks. Cancelled, postponed, and rescheduled events are left out |
| `v_artist_sellout_proxy`, `v_venue_sellout_proxy` | Median hours-to-off-sale per headliner and per venue (also proxies) |
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
