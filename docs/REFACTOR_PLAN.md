# Refactor plan: repo structure (Phase 1 audit)

Status: **proposal, waiting for approval.** Nothing has been moved yet. Branch: `refactor/structure`.

Goal: reorganize the repo to standard structure **without changing behavior**. pouchit.net (and
presalewatch.pages.dev) must look and work exactly the same after every commit.

Contents:

1. [Current file tree](#1-current-file-tree)
2. [Stack summary](#2-stack-summary)
3. [Problems found](#3-problems-found)
4. [Secrets scan](#4-secrets-scan)
5. [Proposed target tree](#5-proposed-target-tree)
6. [Move map](#6-move-map-old-path--new-path)
7. [How each phase is verified](#7-how-each-phase-is-verified)
8. [Risks and open questions](#8-risks-and-open-questions)

---

## 1. Current file tree

105 tracked files. Line counts are in brackets for code files over 200 lines.

### Root

| File | Purpose |
|---|---|
| `pipeline.py` [436] | Data refresh: pulls Ticketmaster presales, Last.fm stats, and SeatGeek prices (paused), scores each event, and writes `data/presales.json` and the `db/*.csv` tables |
| `edge.py` [227] | The Profit % model: fee-adjusted formula, demand signals, demand score, percentile tiers, Unrated, and live-vs-estimated mode. Pure functions |
| `calibrate.py` [241] | Scores past estimates against actual resale 7 and 14 days after on-sale, and refits weights and tiers (`--refit`) |
| `build.py` [207] | Builds `site/`: inlines the data into the page template, writes `alerts.json` and the security headers (CSP hashes), and copies the account pages |
| `common.py` [213] | Grab bag: paths, env and secret reading, the HTTP client with retries, CSV table I/O, time and number parsing |
| `ticketmaster.py` [268] | Ticketmaster Discovery API client: upcoming presales, face values, venues, tour sizes |
| `lastfm.py` | Last.fm API client: listener and play counts, tags, similar artists |
| `seatgeek.py` | SeatGeek API client: resale listing stats (paused: the free tier has no prices) |
| `README.md` [458] | Setup, how edge works, deploy, accounts and alerts, security |
| `CLAUDE.md` | Instructions for Claude Code: scope, product rules, repo map, workflow |
| `SPEC.md` | The original MVP spec (a demo page with no accounts); outdated |
| `EDGE_SPEC.md` | The original Profit % spec; marked as superseded by the README |
| `package.json`, `package-lock.json` | Node dev dependencies (TypeScript, Wrangler, types) and the `test`, `typecheck`, and `audit` scripts |
| `tsconfig.json` | TypeScript settings for the API, the Worker, and the JS tests |
| `wrangler.toml` | Cloudflare Pages project: output dir `site`, public vars, D1 binding, migrations dir |
| `.gitignore` | Ignores `.env`, generated `db/`, `data/`, `site/`, `node_modules/`, Wrangler state |
| `.githooks/pre-commit` | Optional local gitleaks secret scan before each commit |

### `config/`

| File | Purpose |
|---|---|
| `config/model.json` | Model settings: fees, weights, tiers, scaling, estimates, ingestion budget, refresh caps |
| `config/security_headers.json` | Security headers and the CSP, used by `build.py` and the API middleware |
| `config/venues.csv` | Hand-entered venue capacities (these always win) |

### `templates/` (website source)

| File | Purpose |
|---|---|
| `templates/index.html` [1158] | The presale page: HTML, about 250 lines of CSS, and about 800 lines of JS, all in one file |
| `templates/static/alerts.html` | My Alerts: sign-in, the inbox screen, and settings |
| `templates/static/auth/confirm.html` | Landing page for the sign-in link |
| `templates/static/privacy.html` | Privacy page |
| `templates/static/unsubscribe.html` | Unsubscribe confirmation page |
| `templates/static/assets/account.css` | Styles for the account pages |
| `templates/static/assets/alerts.js` [336] | My Alerts script: sign-in, code entry, settings, follows, suggestions |
| `templates/static/assets/confirm.js` | Confirm-page script ("Sign in as …") |
| `templates/static/assets/unsubscribe.js` | Unsubscribe-page script |

### `functions/api/` (Cloudflare Pages Functions: the accounts API)

| File | Purpose |
|---|---|
| `_middleware.ts` | CSRF and Origin checks, security headers, last-resort error handler |
| `auth/request.ts` | Sends the sign-in link and code |
| `auth/code.ts` | Signs in with the 6-digit code |
| `auth/verify.ts` | Previews and confirms the sign-in link |
| `auth/resend.ts` | Sends a fresh link and code |
| `auth/logout.ts` | Ends the session |
| `me.ts` | Gets and deletes the signed-in account |
| `preferences.ts` | Saves alert settings |
| `follows/index.ts`, `follows/[id].ts` | Lists, adds, and removes followed artists |
| `unsubscribe/index.ts`, `unsubscribe/one-click.ts` | Unsubscribe page handler and RFC 8058 one-click |

### `src/lib/` (TypeScript shared by the API and the Worker)

| File | Purpose |
|---|---|
| `crypto.ts` | Random tokens, SHA-256, HMAC, constant-time compare |
| `email.ts` [246] | Email templates (sign-in, welcome, digest) **and** sending through Resend |
| `http.ts` | The `Env` type, JSON responses, error handling, safe logging |
| `match.ts` | Alert matching: which events go into whose digest. Pure |
| `ratelimit.ts` | Fixed-window rate limits stored in D1 |
| `session.ts` | Session cookies and lifetimes |
| `signin.ts` | Sign-in requests: codes, the pending cookie, completing sign-in, welcome email |
| `turnstile.ts` | Turnstile verification (Cloudflare API call) |
| `unsubscribe.ts` | Signed unsubscribe tokens and links |
| `validate.ts` | Input validation |

### `worker/` (the hourly alert Worker)

| File | Purpose |
|---|---|
| `worker/src/index.ts` | Hourly cron: reads `alerts.json`, matches users, sends digests (dry-run now) |
| `worker/wrangler.toml` | Worker config: cron, vars (`DRY_RUN`, `POSTAL_ADDRESS`), D1 binding |

### `ingest/` (daily jobs that fill the D1 intelligence database)

| File | Purpose |
|---|---|
| `run.py` | Runs the jobs in order, with the daily write budget |
| `db.py` [295] | Database layer: local SQLite or D1 over HTTP, upsert helpers, run log, write counting |
| `resolve.py` | Matching rules for artists and venues across sources (names, IDs, distance) |
| `ticketmaster_job.py` | Events, venues, artists, presales, status history into D1 |
| `lastfm_job.py` | Listeners, plays, tags, similar artists |
| `musicbrainz_job.py` | MusicBrainz IDs and details (contains its own API client) |
| `wikidata_job.py` | Wikidata items, Wikipedia titles, YouTube channel IDs (contains its own API client) |
| `venue_enrichment.py` [641] | Venue capacities from Wikidata, Wikipedia, and OpenStreetMap: **three API clients, matching, and DB writes in one file** |
| `venue_rules.py` | Pure venue matching and capacity-parsing rules |
| `venue_estimates.py` | Estimated capacities for small venues |
| `venue_handfill.py` | Exports and imports the hand-fill CSV |
| `catchment.py` | Population within 80 km of each venue (Census download inside) |
| `pageviews_job.py` | Weekly Wikipedia pageviews (contains its own API client) |
| `listenbrainz_job.py` | ListenBrainz listener totals (contains its own API client) |
| `youtube_job.py` | YouTube channel statistics (contains its own API client) |
| `mediawiki.py` | GET helper for Wikidata and Wikipedia that handles maxlag |
| `coverage.py` | Coverage report printed after each run |
| `import_prices.py` | Imports hand-logged prices from a CSV |
| `__init__.py` | Package marker |

### `migrations/` (D1 schema, applied by Wrangler in order)

| File | Purpose |
|---|---|
| `0001_accounts_alerts.sql` | Users, preferences, follows, sign-in tokens, sessions, sent alerts, rate limits |
| `0002_intel.sql` [495] | Intelligence database: artists, venues, events, presales, prices, views |
| `0003`–`0009` | Demand sources, venue enrichment, estimates, MBID rejection, write budget, sign-in codes, follow and welcome |

### `tests/`

| File | Purpose |
|---|---|
| `tests/test_*.py` (10 files) | Python unit tests: edge, build, schema, ingest jobs, venues, estimates, catchment, write budget, Last.fm, MediaWiki |
| `tests/future_clock.py` | Runs the Python suite with the clock moved forward (CI) |
| `tests/__init__.py` | Package marker |
| `tests/js/*.test.ts` (10 files) | API, auth, sign-in code, follow and welcome, CSRF, email, matching, rate limits, Worker, source guards |
| `tests/js/helpers.ts`, `tests/js/fixtures.ts` | Fake D1, fake fetch, route caller, test data |
| `fixtures/sample_events.json` [10,295] | A saved Ticketmaster API response, used by the Ticketmaster tests |

### `docs/` and `.github/`

| File | Purpose |
|---|---|
| `docs/database.md` [454] | Every table, field, and view in the intelligence database, plus each source's terms |
| `docs/observed_prices_template.csv` | Template for hand-logged prices |
| `.github/workflows/tests.yml` | PR checks: Python tests (also at future dates), typecheck, JS tests, audit, gitleaks |
| `.github/workflows/deploy.yml` | Every 6 hours and on merge: tests, pipeline, build, deploy to Pages, failure alert |
| `.github/workflows/ingest.yml` | Daily ingestion into D1 and the hand-fill CSV |
| `.github/workflows/backend.yml` | On merge: applies D1 migrations and deploys the alert Worker |
| `.github/workflows/watchdog.yml` | Hourly: alerts if the site is unreachable or its data is 8+ hours old |
| `.github/scripts/ops-alert.sh` | Opens and closes ops-alert issues |

---

## 2. Stack summary

The spec template assumes a JS front-end framework. This project doesn't use one.

| Layer | What it actually is |
|---|---|
| **Framework** | **None.** The page is plain HTML, CSS, and vanilla JS. `build.py` (Python) inlines the data and computes CSP hashes |
| **Build tool** | `python build.py`, which writes `site/`. No bundler. TypeScript is not compiled by us: Cloudflare (Wrangler) bundles Functions and the Worker, and Node 24 runs the `.ts` tests directly |
| **Data pipeline** | Python 3.11, **standard library only** (no `requirements.txt`). Runs `pipeline.py`, `calibrate.py`, and `build.py` in GitHub Actions every 6 hours |
| **Ingestion** | Python `ingest/` package, daily in GitHub Actions. Writes to D1 over Cloudflare's HTTP API |
| **Hosting** | Cloudflare Pages project `presalewatch` (domains: `presalewatch.pages.dev`, `pouchit.net`). **Direct upload, no Git connection** (checked with the Cloudflare API: `source: None`). `deploy.yml` runs `wrangler pages deploy site` |
| **Server code** | Pages Functions in `functions/api/` (accounts API), plus a separate Worker `presalewatch-alerts` (hourly cron) in `worker/`. Shared TS lives in `src/lib/` |
| **Storage** | One Cloudflare D1 database `presalewatch`: accounts tables plus the intelligence database. Generated CSV and JSON live on the `data` git branch |
| **Data sources** | Live page: Ticketmaster Discovery (presales, face values), Last.fm (listeners), SeatGeek (resale, paused). Ingestion adds MusicBrainz, ListenBrainz, Wikidata, Wikipedia (infoboxes and pageviews), YouTube, OpenStreetMap Overpass, and the US Census |
| **Email** | Resend (sign-in from the API, digests from the Worker) |
| **Bot check** | Cloudflare Turnstile on the sign-in form |
| **Tests** | Python `unittest` (152 tests), Node test runner (120 tests), `tsc --noEmit`, `npm audit`, gitleaks |

---

## 3. Problems found

### Files over ~300 lines

| File | Lines | Why it's large | Proposal |
|---|---|---|---|
| `templates/index.html` | 1158 | HTML, CSS, and JS in one file | Split into the HTML shell, `presales.css`, and about 10 small JS modules (tours, formatting, status, tooltip, cards, alert-me, filters, countdown, URL state, main). `build.py` stitches them back into the **same single inline output**, so the built page is byte-identical and the CSP doesn't change |
| `ingest/venue_enrichment.py` | 641 | Three API clients (Wikidata, Wikipedia, Overpass), candidate building, matching, DB writes, and the OSM pass | Clients go to `services/`, candidates to `ingest/venues/candidates.py`, and the orchestration stays (about 250 lines) |
| `pipeline.py` | 436 | Orchestration, artist and venue upserts, D1 capacity lookup, resale pull, prediction log, and row building | Split into `refresh/` steps: `sources.py`, `enrich.py`, `score.py`, `output.py`, and a short `main` |
| `templates/static/assets/alerts.js` | 336 | Sign-in, code entry, settings, follows, suggestions | Split into `signin.js`, `settings.js`, `follows.js`, and `alerts.js` (entry) |
| `migrations/0002_intel.sql` | 495 | Schema | **Keep.** Applied migrations must never change |
| `tests/test_venues.py`, `tests/test_ingest.py` | 404, 333 | Many small tests | Keep, or split by topic if it helps (optional) |
| `fixtures/sample_events.json` | 10,295 | Recorded API response | Keep (data) |

### Mixed concerns

- **`common.py`** mixes paths, env and secrets, the HTTP client, CSV I/O, and parsing helpers. Proposal: split into `config.py`, `http.py`, `tables.py`, and `parse.py`.
- **`src/lib/email.ts`** mixes three templates with the Resend client. Proposal: templates go to `src/emails/`, sending to `src/services/resend.ts`.
- **`src/lib/http.ts`** mixes the `Env` type (config) with response helpers. Proposal: the `Env` type moves to `src/config/env.ts`.
- **`src/lib/signin.ts`** mixes request logic with sending the welcome email. Proposal: the welcome trigger stays, and the email build and send go through the email and service modules.

### API clients scattered (spec: "one client per external service")

- **Separate clients already:** Ticketmaster, Last.fm, and SeatGeek have their own modules (`ticketmaster.py`, `lastfm.py`, `seatgeek.py`).
- **Clients embedded in job files:**
  - MusicBrainz: `musicbrainz_job.py`
  - ListenBrainz: `listenbrainz_job.py`
  - Wikidata: in **two** places, `wikidata_job.py` and `venue_enrichment.py`, with `WIKIDATA_API` defined twice
  - Wikipedia: `venue_enrichment.py`
  - Pageviews: `pageviews_job.py`
  - YouTube: `youtube_job.py`
  - Overpass: `venue_enrichment.py`
  - Census: `catchment.py`
  - D1 HTTP: `ingest/db.py`
- **Two HTTP stacks:**
  - `common.Http`, with retries and backoff;
  - raw `urllib.request.urlopen` in ListenBrainz, Census, Overpass status, and D1, each with its own retry logic or none.

  Proposal: one shared HTTP client, and one module per service under `services/`.
- **TypeScript:**
  - `fetch` is already contained in `email.ts` (Resend) and `turnstile.ts`, so moving Resend to `services/resend.ts` finishes this.
  - The page scripts call only our own `/api/*`.

### Duplicates

- **`User-Agent`** is defined 7 times with 4 spellings (for example `"PouchIt/1.0 (https://pouchit.net)"` and `"PouchIt/1.0 ( https://pouchit.net )"`). Proposal: define it once in config. The exact strings sent today differ slightly by service (MusicBrainz asks for the `( url )` form), so I'll keep each service's current string to stay behavior-identical, but defined in one place.
- **`WIKIDATA_API`** is defined twice.
- **The D1 database ID** (`b3dc330c-…`) appears 3 times: `wrangler.toml`, `worker/wrangler.toml`, and `ingest/db.py`.
- **Retry and backoff loops** are written 4 times.

### Hardcoded values

| Value | Where | Proposal |
|---|---|---|
| Turnstile site key `0x4AAAAAAFJpsdv0jmtHdce7` | `alerts.html` | Public by design, but move it to config and inject it at build |
| `https://pouchit.net` | 7 Python modules (in User-Agents), privacy page, tests | One `SITE_ORIGIN` setting (Python config; `APP_ORIGIN` already exists for TS) |
| D1 database ID | `ingest/db.py` | Read it from `wrangler.toml` (one source of truth) or env `D1_DATABASE_ID` |
| `presalewatch` Pages project name | `deploy.yml`, `backend.yml` | Already an env var in `deploy.yml` (`CF_PROJECT`). Use it in both |
| API base URLs | Each client | Fine as constants **inside** their service module (that's the standard) |

Non-secret settings (weights, fees, tiers) already live in `config/model.json`. That's the right place, and it isn't "hardcoded" in the problem sense.

### Inconsistent naming

- **Python ingest jobs:** `*_job.py` (`lastfm_job.py`) sits next to `venue_enrichment.py` and `catchment.py`. Proposal: a `jobs/` folder named by source (`jobs/lastfm.py`), with services in `services/lastfm.py`.
- **JS/TS file names:** mostly single lowercase words, plus `one-click.ts` and `follow-welcome.test.ts`. Proposal: kebab-case for all TS/JS files, snake_case for Python, and upper-case root docs (`README.md`, `CLAUDE.md`, `CONTRIBUTING.md`).
- **Product name:** the repo, the Pages project, and the Worker are still named `presalewatch`, while the product is PouchIt. **Not proposing renames:** changing the Pages project or Worker name would create new Cloudflare resources and break the domain setup.

### Missing or uneven error handling

- `ingest/catchment.py` downloads the Census file with no retry. A single timeout fails the job (only on days it has new venues).
- `listenbrainz_job.py` and `musicbrainz_job.py` have their own retry rules that differ from `common.Http`. The shared client would standardize them, but **I'll keep each service's current timing**, since several sources ask for specific rates.
- These are deliberate and fine:
  - `alerts.js` and `index.html` silently ignore a failed `/alerts.json` or `/api/me` (the page falls back to signed-out), which is right.
  - `pipeline.py` warns and continues when D1 is unreachable, by design.

### Dead code and stale files

- **No unused functions or exports.** I checked every top-level Python name and every exported TS name.
- **Stale documents:**
  - `SPEC.md` describes the original demo (no accounts or alerts), so it no longer matches the product;
  - `EDGE_SPEC.md` is marked superseded.

  Proposal: move both to `docs/specs/` with a "superseded" note, or delete them (your call; see open questions).
- **Unused committed files:** none found.

---

## 4. Secrets scan

**Result: no secrets found in code, config, or git history.**

- **Scope:** all 92 commits on all branches (including `data`), from `git log --all -p`.
- **Patterns checked:**
  - Google/YouTube keys (`AIza…`);
  - Resend keys (`re_…`);
  - Turnstile secrets (`0x4AAAAAAA…`);
  - Cloudflare bearer tokens;
  - `key=`/`token=`/`client_id=` in URLs;
  - quoted assignments to `api_key`/`secret`/`token`/`password`;
  - any 32-character hex string (the Ticketmaster and Last.fm key shape).
- **What matched:**
  - two test placeholders (`"turnstile_test_secret"`, `"re_test_key_not_real"`);
  - one 32-hex string, which is a venue's public ticket URL in the presale data (`theorientaltheater.holdmyticket.com/tickets-preview/5fd5…`).
- **`.env` / `.dev.vars`:** never committed, and both are git-ignored.
- **Public values in the repo (not secrets):**
  - the Turnstile **site** key (public by design);
  - the D1 database ID (an identifier; access needs a token);
  - the domains.

  The Cloudflare account ID is not in the repo.
- **Ongoing protection:** CI runs gitleaks over the full history on every PR (passing), and there's an optional local pre-commit hook.
- **Keys to rotate:** none, based on this scan.

---

## 5. Proposed target tree

Adapted to the real stack (Python pipeline, TS server code, plain static site). No empty folders.

```
/
├── functions/api/            # Pages Functions (MUST stay at the repo root: `wrangler pages deploy` looks for ./functions)
├── src/                      # TypeScript shared by the API and the Worker
│   ├── config/env.ts         # the Env type: every variable the API and Worker read
│   ├── lib/                  # business logic: match, validate, session, signin, ratelimit, crypto, unsubscribe, http (responses)
│   ├── emails/               # email templates: signin.ts, welcome.ts, digest.ts, shared.ts
│   └── services/             # external clients: resend.ts, turnstile.ts
├── workers/alerts/           # the alert Worker (was worker/); its name in wrangler.toml stays presalewatch-alerts
├── web/                      # website source (was templates/)
│   ├── pages/                # index.html, alerts.html, privacy.html, unsubscribe.html, auth/confirm.html
│   ├── scripts/              # presales/*.js (split from index.html), alerts/*.js, confirm.js, unsubscribe.js
│   └── styles/               # presales.css, account.css
├── pouchit/                  # Python package: everything Python except tests
│   ├── config.py             # paths, env and secret access, config/*.json, User-Agents, site origin, D1 ID (one place)
│   ├── http.py               # the one HTTP client (retries, backoff)
│   ├── tables.py             # CSV table I/O
│   ├── parse.py              # number, date, and URL helpers
│   ├── model/                # pure scoring: edge.py, calibrate.py (math only)
│   ├── services/             # one client per API: ticketmaster, lastfm, seatgeek, musicbrainz, listenbrainz,
│   │                         #   wikidata, wikipedia, pageviews, youtube, overpass, census, d1
│   ├── refresh/              # the 6-hourly page data refresh (was pipeline.py, split into steps)
│   ├── site/                 # site build (was build.py)
│   └── ingest/               # daily D1 jobs: run.py, db.py, coverage.py, rules/ (resolve, venue_rules), jobs/<source>.py
├── scripts/                  # thin entry points: refresh.py, build_site.py, calibrate.py, ingest.py, venue_handfill.py, import_prices.py
├── migrations/               # D1 schema (unchanged; wrangler.toml points here)
├── config/                   # model.json, security_headers.json, venues.csv (unchanged: read by Python and TS)
├── tests/
│   ├── python/               # test_*.py, future_clock.py
│   ├── js/                   # *.test.ts, helpers
│   └── fixtures/             # sample_events.json
├── docs/                     # ARCHITECTURE.md, database.md, REFACTOR_PLAN.md, templates/, specs/ (archived)
├── .github/                  # workflows and scripts
├── .env.example              # every variable the Python jobs read
├── .dev.vars.example         # every secret the Pages Functions need locally
├── README.md, CLAUDE.md, CONTRIBUTING.md
├── wrangler.toml, package.json, tsconfig.json
└── (generated, ignored) site/, data/, db/
```

Choices worth calling out:

- **`functions/` stays at the root.** `wrangler pages deploy site` picks up `./functions` from the working directory.
- **`site/` stays the output folder,** so `wrangler.toml` (`pages_build_output_dir`), the deploy command, and the Pages project don't change.
- **`config/`, `migrations/`, and the `data` branch layout (`db/`, `data/`) stay put.** Wrangler and both languages read them, and the data branch must keep loading.
- **No `public/` folder.** There are no static assets beyond the pages, CSS, and JS, which `web/` covers.
- **Python is a package (`pouchit/`) run with `python -m` or the `scripts/` entry points.** This works the same in PowerShell and in Linux CI with no install step. It stays standard-library only.

---

## 6. Move map (old path → new path)

Every move uses `git mv`, so history and blame follow the files. "Split" means the code is divided across new files without changing what it does.

### Python

| Old | New |
|---|---|
| `common.py` | split → `pouchit/config.py` (paths, env, secrets, `load_config`), `pouchit/http.py` (`Http`, `ApiError`), `pouchit/tables.py` (CSV), `pouchit/parse.py` (num, iso, parse_utc, …) |
| `edge.py` | `pouchit/model/edge.py` |
| `calibrate.py` | `pouchit/model/calibrate.py` (math) + `scripts/calibrate.py` (CLI) |
| `pipeline.py` | split → `pouchit/refresh/{sources,enrich,score,output}.py` + `scripts/refresh.py` |
| `build.py` | `pouchit/site/build.py` + `scripts/build_site.py` |
| `ticketmaster.py` | `pouchit/services/ticketmaster.py` |
| `lastfm.py` | `pouchit/services/lastfm.py` |
| `seatgeek.py` | `pouchit/services/seatgeek.py` |
| `ingest/run.py` | `pouchit/ingest/run.py` + `scripts/ingest.py` |
| `ingest/db.py` | `pouchit/ingest/db.py`; its D1 HTTP part → `pouchit/services/d1.py` |
| `ingest/resolve.py` | `pouchit/ingest/rules/resolve.py` |
| `ingest/venue_rules.py` | `pouchit/ingest/rules/venue_rules.py` |
| `ingest/mediawiki.py` | `pouchit/services/mediawiki.py` (shared by wikidata and wikipedia) |
| `ingest/ticketmaster_job.py` | `pouchit/ingest/jobs/ticketmaster.py` |
| `ingest/lastfm_job.py` | `pouchit/ingest/jobs/lastfm.py` |
| `ingest/musicbrainz_job.py` | `pouchit/ingest/jobs/musicbrainz.py`; client → `pouchit/services/musicbrainz.py` |
| `ingest/listenbrainz_job.py` | `pouchit/ingest/jobs/listenbrainz.py`; client → `pouchit/services/listenbrainz.py` |
| `ingest/wikidata_job.py` | `pouchit/ingest/jobs/wikidata.py`; client → `pouchit/services/wikidata.py` |
| `ingest/pageviews_job.py` | `pouchit/ingest/jobs/pageviews.py`; client → `pouchit/services/pageviews.py` |
| `ingest/youtube_job.py` | `pouchit/ingest/jobs/youtube.py`; client → `pouchit/services/youtube.py` |
| `ingest/venue_enrichment.py` | `pouchit/ingest/jobs/venues.py` + `pouchit/ingest/venues/candidates.py`; clients → `pouchit/services/{wikidata,wikipedia,overpass}.py` |
| `ingest/venue_estimates.py` | `pouchit/ingest/jobs/estimates.py` |
| `ingest/catchment.py` | `pouchit/ingest/jobs/catchment.py`; download → `pouchit/services/census.py` |
| `ingest/coverage.py` | `pouchit/ingest/coverage.py` |
| `ingest/venue_handfill.py` | `pouchit/ingest/venue_handfill.py` + `scripts/venue_handfill.py` |
| `ingest/import_prices.py` | `pouchit/ingest/import_prices.py` + `scripts/import_prices.py` |
| `ingest/__init__.py` | `pouchit/ingest/__init__.py` (plus `__init__.py` in each new package) |

### TypeScript

| Old | New |
|---|---|
| `src/lib/email.ts` | split → `src/emails/{signin,welcome,digest,shared}.ts` + `src/services/resend.ts` |
| `src/lib/turnstile.ts` | `src/services/turnstile.ts` |
| `src/lib/http.ts` | `src/lib/http.ts` (responses) + `src/config/env.ts` (the `Env` type) |
| `src/lib/{crypto,match,ratelimit,session,signin,unsubscribe,validate}.ts` | unchanged |
| `worker/src/index.ts` | `workers/alerts/src/index.ts` |
| `worker/wrangler.toml` | `workers/alerts/wrangler.toml` (same `name`, cron, and bindings) |
| `functions/**` | unchanged (only import paths update) |

### Website

| Old | New |
|---|---|
| `templates/index.html` | split → `web/pages/index.html` (shell) + `web/styles/presales.css` + `web/scripts/presales/*.js` |
| `templates/static/alerts.html` | `web/pages/alerts.html` |
| `templates/static/auth/confirm.html` | `web/pages/auth/confirm.html` |
| `templates/static/privacy.html` | `web/pages/privacy.html` |
| `templates/static/unsubscribe.html` | `web/pages/unsubscribe.html` |
| `templates/static/assets/account.css` | `web/styles/account.css` (still published as `/assets/account.css`) |
| `templates/static/assets/alerts.js` | split → `web/scripts/alerts/*.js` (published as one `/assets/alerts.js`, same content) |
| `templates/static/assets/confirm.js` | `web/scripts/confirm.js` (published as `/assets/confirm.js`) |
| `templates/static/assets/unsubscribe.js` | `web/scripts/unsubscribe.js` (published as `/assets/unsubscribe.js`) |

The **published URLs don't change** (`/alerts`, `/assets/alerts.js`, …). `build.py` maps the new source paths to the same output paths.

### Tests, docs, and the rest

| Old | New |
|---|---|
| `tests/test_*.py`, `tests/future_clock.py`, `tests/__init__.py` | `tests/python/` |
| `tests/js/*` | unchanged |
| `fixtures/sample_events.json` | `tests/fixtures/sample_events.json` |
| `SPEC.md`, `EDGE_SPEC.md` | `docs/specs/` (with a superseded note), or deleted (your call) |
| `docs/observed_prices_template.csv` | `docs/templates/observed_prices.csv` |
| (new) | `.env.example`, `.dev.vars.example`, `docs/ARCHITECTURE.md`, `CONTRIBUTING.md` |
| `.github/workflows/*.yml` | updated commands and paths only (see verification) |

### Commands that change

| Before | After |
|---|---|
| `python pipeline.py` | `python scripts/refresh.py` |
| `python calibrate.py [--refit]` | `python scripts/calibrate.py [--refit]` |
| `python build.py` | `python scripts/build_site.py` |
| `python -m ingest.run --db … --sources …` | `python scripts/ingest.py --db … --sources …` |
| `python -m ingest.venue_handfill …` | `python scripts/venue_handfill.py …` |
| `python -m unittest` | `python -m unittest discover -s tests/python -t .` (also `npm test` runs both) |
| `--config worker/wrangler.toml` | `--config workers/alerts/wrangler.toml` |

---

## 7. How each phase is verified

The refactor's promise is "behaves exactly the same", so the checks compare outputs, not just "tests pass".

1. **Tests:** 152 Python and 120 JS tests pass after every commit, the same count. Nothing is deleted to make them pass.
2. **Byte-identical site.** Before starting, I build `site/` from a frozen copy of `data/presales.json` and keep it as the "golden" copy. After every commit that touches the site or its build, I rebuild from the same data and diff every file. The pages, `alerts.json`, and `_headers` (including CSP hashes) must be **identical**. This is why `index.html` is split into source files but stitched back into the same inline output.
3. **Identical scores.** I run the scoring step (`edge` plus percentile tiers) on a frozen snapshot of today's events and venue capacities before and after. `presales.json` must match field for field. The live API calls aren't part of this comparison.
4. **Ingestion:** each job runs against a copy of the local intelligence database, before and after, with recorded API responses where available. The rows written must match.
5. **API and Worker:** `tsc --noEmit` plus the JS tests (which run every route through the real middleware). `wrangler deploy --dry-run` for the Worker checks the bundle builds from its new path.
6. **Local run:** `python scripts/build_site.py` and `npx wrangler pages dev site`. I click through every page (presales, filters, tooltips, Alert me, My Alerts sign-in screens, confirm, unsubscribe, privacy) in a browser.
7. **Cloudflare preview:** deploy the branch to a Pages **preview** URL (`wrangler pages deploy site --branch refactor-structure`) and compare it with production page by page. See the risk note about preview and the production database.

---

## 8. Risks and open questions

### Risks

| Risk | How it's handled |
|---|---|
| **Python paths:** `common.py` computes the repo root from its own location (`ROOT = Path(__file__).parent`). Moving it would silently point `db/`, `data/`, and `config/` at the wrong folder | `pouchit/config.py` computes the root explicitly (two levels up), and a test asserts it finds `config/model.json` and `migrations/` |
| **Workflows and the `data` branch:** `deploy.yml` loads `db/` and `data/` from the `data` branch into the repo root and saves them back | Layout unchanged. Workflow commands updated in the same commit as the code they call; CI runs on the PR |
| **The Worker:** if its `name` in `wrangler.toml` changed, Cloudflare would create a second Worker, and both would send alerts | The name, cron, and bindings stay identical; only the folder moves. `backend.yml` gets the new `--config` path |
| **Pages Functions** must stay in `./functions` | They do |
| **CSP hashes:** any byte change in inline scripts or styles changes the hashes | Computed at build anyway, and the byte-identical check proves no change |
| **The Cloudflare preview uses the production D1 database.** `wrangler.toml` has one D1 binding for production and previews, so signing in on a preview URL would write real accounts | Compare the preview's static pages and signed-out behavior only, and don't sign in on the preview. Turnstile would reject the preview hostname anyway. A separate preview database is possible but is new infrastructure, which is out of scope |
| **Open branches:** PR #15 (concert capacity + analytics CSP) touches files that move | Merge or close #15 first, or I rebase it after the move |
| **The formatting commit (Phase 3)** touches nearly every file | It's one dedicated commit with no logic changes, done last, after all other branches are merged |
| **Windows:** paths with `\` vs `/`, and `python` vs `python3` | Scripts use `pathlib` only and are run as `python scripts/x.py`. CI covers Linux; I run every command in PowerShell here |
| **Size:** the Python reorganization touches most files | About 25–30 small commits, each one move or split with its tests passing. `git mv` keeps history |

### Open questions (I'll assume the first option unless you say otherwise)

1. **`SPEC.md` and `EDGE_SPEC.md`:** move to `docs/specs/` with a "superseded" note, **or** delete?
2. **Python package name:** `pouchit/`, **or** `pipeline/`, **or** `presalewatch/`? I suggest `pouchit` (the product name), even though the Cloudflare resources keep `presalewatch`.
3. **Python lint and format (Phase 3):** **Ruff** (free, one dev tool via `pip install ruff`, not needed at runtime), **or** none and only ESLint + Prettier for TS/JS? The spec asks for "the stack's equivalent", and Ruff is it for Python.
4. **Splitting `index.html` into ~10 JS modules:** they're concatenated back into one inline script, so behavior and CSP are unchanged. Okay, **or** keep the JS in one file and only move the CSS out?
5. **Preview check:** compare static pages and signed-out behavior on a preview URL, **or** add a separate preview D1 database so sign-in can be tested there too (new resource, more setup)?
6. **`dev` script:** `npm run dev` = build the site, then `wrangler pages dev site` (local API with a local D1 and a `.dev.vars` file). Okay?

**Nothing else changes until you approve this plan.**
