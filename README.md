# PouchIt

A shareable page listing upcoming US concert presales, ranked by **edge**:
the expected resale return per dollar spent, after seller and primary fees.
Free accounts get email alerts (see [Accounts and alerts](#accounts-and-alerts)).
Live at https://pouchit.net.
See [EDGE_SPEC.md](EDGE_SPEC.md) for the edge model and [SPEC.md](SPEC.md) for the original MVP.

| File | What it does |
| --- | --- |
| `pipeline.py` | Pulls Ticketmaster, Last.fm, and SeatGeek; computes edge; updates `db/*.csv`; writes `data/presales.json` |
| `calibrate.py` | Scores past predictions against real resale; `--refit` retunes the model |
| `build.py` | Renders `site/`: the main page (data inlined), `alerts.json`, account pages, and `_headers` |
| `edge.py` | The Profit % formula, demand score, and tiers (pure functions) |
| `ticketmaster.py`, `lastfm.py`, `seatgeek.py` | One module per data source |
| `config/model.json` | Fees, weights, tier cutoffs, and multiple ranges |
| `config/venues.csv` | Venue capacities, entered by hand. Seeded for NYC |
| `db/`, `data/` | Generated tables and `presales.json`. Kept on the `data` branch, not `main` |
| `ingest/` | Daily jobs filling the D1 intelligence database (see [docs/database.md](docs/database.md)). They don't affect the live page yet |

The data pipeline uses only the Python standard library, so there is nothing to `pip install`.
The accounts API and alerts are TypeScript on Cloudflare; see [Accounts and alerts](#accounts-and-alerts).

## How edge works

```
Profit % = (resale × (1 − seller fee) − (face + primary fees)) ÷ (face + primary fees)
```

Fees default to 15% seller and 25% of face for primary (`config/model.json`).

- **Live (Mode A):** when SeatGeek has 10+ listings and Ticketmaster lists a face
  value, resale is the median listing less `ask_to_sale_discount` (default 15%),
  giving an exact Profit %. Listings are asking prices and tickets usually sell
  below the ask, so the page labels Live edges "based on asking prices".
  Calibration applies the same discount to the SeatGeek medians it scores against.
  **Paused:** SeatGeek's free tier returns events with an empty `stats` object (no
  listing count or prices), so `refresh.seatgeek_enabled` is `false` in
  `config/model.json` and scheduled runs skip SeatGeek. Every row is an estimate
  until a resale source with prices is available.
- **Estimated (Mode B):** otherwise a demand score picks a High/Med/Low tier and a
  resale multiple range, shown as a margin range such as "+22% to +104%". The score
  is a weighted average of these signals, each scaled 0 to 1 (`config/model.json`):

  | Signal | Source | Weight |
  | --- | --- | --- |
  | Listeners (log scale, 10K to 3.2M) | Last.fm | 0.15 |
  | Plays per listener (log scale, 3 to 100) | Last.fm | 0.10 |
  | **Listeners per venue seat** (log scale) | Last.fm + venue capacity | **0.50** |
  | Scarcity, 1 ÷ tour dates | Ticketmaster | 0.10 |
  | Market: people within 80 km of the venue (log scale, 250K to 20M) | 2020 Census | 0.075 |

  Listeners per seat dominates: a big audience for a small room is what drives resale
  prices, so raw popularity counts for less. A missing signal is left out and the
  other weights are rescaled to add up to 1, so a gap doesn't pull the score toward
  a made-up middle value. Confidence is Med. (Weights are relative: the five add up to
  0.925, and the score divides by the total.)

  **Tiers are percentiles.** Each run ranks the rated events that have a venue capacity
  by score: the **top 10% are High, the next 40% Med**, the rest Low. A venue with an
  estimated capacity is ranked as if it had 3,000 seats. With fewer than 50 such events
  the fixed cutoffs in `config/model.json` apply instead (High 0.70, Med 0.45). Settings
  are in `tiering`.

  **No Last.fm data means Unrated.** Without listener data the score would rest on
  market and tour size alone, which can't show that people want the tickets, so the
  event gets no tier and no estimated range. The page shows it as "Unrated" and sorts
  it below rated events.

  **High demand needs a venue capacity too.** Without it the rescaled weights fall on
  market and raw popularity, which overrate big-market shows and big artists in big
  rooms, so such events are capped at Med.

  **Where the capacity comes from**, in order: the hand-entered value in
  `config/venues.csv`; a measured capacity from the intelligence database (Wikidata,
  Wikipedia, or OpenStreetMap; see [docs/database.md](docs/database.md)); then, when
  `capacity_estimate.enabled` is on, an **estimated** capacity for small venues no source
  covers. An estimate counts for less: it can make an event High only if the show would
  still rate High in a 3,000-seat room, and the page labels that rating **High demand ·
  est.** Rooms inside resorts and casinos never get an estimate: hotel, resort, and casino
  names are filtered out, and rooms whose names don't say so (The Cosmopolitan, Fontainebleau)
  are listed in `capacity_estimate.hand_fill_venues`, which also puts them at the top of the
  hand-fill list.

Edge doesn't adjust for the odds of actually getting tickets. Ticketmaster rarely
publishes price ranges before on-sale, so most rows start as estimates. The pipeline
backfills face values for recent events after their presales end, which Mode A and
calibration both need.

## Windows setup

1. Install **Python 3.11+** from [python.org](https://www.python.org/downloads/windows/)
   (tick "Add python.exe to PATH"), then check it in PowerShell:

   ```powershell
   python --version
   ```

2. Get a free API key at [developer.ticketmaster.com](https://developer.ticketmaster.com/)
   (My Apps → your app → **Consumer Key**).

3. Optional, for demand signals: a Last.fm API key from
   [last.fm/api/account/create](https://www.last.fm/api/account/create). Without it the
   pipeline still runs, but every row is a Low-confidence estimate. (SeatGeek is paused;
   see Live mode above.)

4. In the project folder, create a file named `.env` containing:

   ```
   TM_API_KEY=your_consumer_key_here
   LASTFM_API_KEY=
   SEATGEEK_CLIENT_ID=
   ```

   `.env` is in `.gitignore`, so it never gets committed. Environment variables
   with the same names work too.

## Run locally

```powershell
python pipeline.py
python calibrate.py
python build.py
start site\index.html
python -m unittest
```

A Ticketmaster fetch uses about a dozen API calls (the free tier allows ~5,000/day).
Last.fm lookups are cached for 7 days per artist, including artists Last.fm doesn't know,
and take about a quarter second each (Last.fm asks for at most ~5 requests a second).
`fixtures/sample_events.json` is a saved raw API response for reference.

A local run starts from empty tables. To start from the live data instead, copy
it from the `data` branch first:

```powershell
git fetch origin data
git archive origin/data | tar -x -f -
```

`db/` and `data/` are ignored on `main`, so this never shows up as a change.

### Adding venue capacities

Every venue seen is listed in `db/venues.csv`; ones without a capacity have a blank
there. To add one, copy its row into `config/venues.csv`, fill in `capacity`, and open a
pull request. Market size isn't entered by hand: it's the population within 80 km of the
venue, from the 2020 Census.

## Calibration

`calibrate.py` runs after every pipeline run. Each estimated event is scored on
the SeatGeek median 7 and 14 days after its public on-sale (`db/scores.csv`), and
`db/calibration.json` reports tier accuracy, share of actuals inside the predicted
range (target 70%), and mean absolute error.

Once 50+ events are scored, the report flags a review monthly or every 25 new
scores. Then run `python calibrate.py --refit`: it fits a log-linear regression of
the actual multiple on the demand signals and rewrites the weights, tier cutoffs,
and ranges in `config/model.json`. (With percentile tiers on, the fitted cutoffs are
only the fallback for small runs.) Copy the data down first (see above), check
the printed tiers, then open a pull request with the new `config/model.json`.

To edit the page, change `templates/index.html` and rerun `python build.py`.
Don't edit `site/index.html` directly, because the build overwrites it.

Pull requests to `main` run the unit tests (`.github/workflows/tests.yml`).
They need no API keys or secrets.

## Deploy to Cloudflare Pages

GitHub Actions fetches and builds the page, then uploads `site/` to Cloudflare
Pages with Wrangler. The repo can be public or private.

One-time setup:

1. **Create a Cloudflare API token.** In the Cloudflare dashboard go to
   **My Profile → API Tokens → Create Token → Create Custom Token**, and add the
   permission **Account → Cloudflare Pages → Edit**. Copy the token.
2. **Find your account ID.** It's under **Workers & Pages** in the dashboard
   (right-hand sidebar, "Account ID").
3. **Add the repo secrets** under **Settings → Secrets and variables → Actions**:

   | Name | Value |
   | --- | --- |
   | `TM_API_KEY` | your Ticketmaster key |
   | `LASTFM_API_KEY` | optional, enables listener counts (the demand signal) |
   | `SEATGEEK_CLIENT_ID` | optional; SeatGeek is paused (no prices on the free tier) |
   | `CLOUDFLARE_API_TOKEN` | the token from step 1 |
   | `CLOUDFLARE_ACCOUNT_ID` | the ID from step 2 |

   Or from PowerShell with the [GitHub CLI](https://cli.github.com/), pasting each value when prompted:

   ```powershell
   gh secret set TM_API_KEY
   gh secret set LASTFM_API_KEY
   gh secret set SEATGEEK_CLIENT_ID
   gh secret set CLOUDFLARE_API_TOKEN
   gh secret set CLOUDFLARE_ACCOUNT_ID
   ```

4. **Run it:** **Actions → Refresh and deploy → Run workflow**, or `gh workflow run deploy.yml`.

The first run creates the `presalewatch` Pages project if it doesn't exist yet,
so there's nothing to set up in Cloudflare beyond the token. If you'd rather
create it yourself (this needs [Node.js](https://nodejs.org/)):

```powershell
$env:CLOUDFLARE_ACCOUNT_ID = "your_account_id"
npx wrangler pages project create presalewatch --production-branch=main
```

(Without `CLOUDFLARE_API_TOKEN` set, Wrangler opens a browser to log you in.)

The site is served at `https://presalewatch.pages.dev`. If that name is already
taken on Cloudflare, you'll get a suffixed subdomain instead; the exact URL is
printed in the deploy step's log and shown under **Workers & Pages** in the dashboard.

The product is called PouchIt, but the Cloudflare Pages project (`presalewatch`), the D1
database (`presalewatch`), and the alert Worker (`presalewatch-alerts`) keep their original
names. Cloudflare can't rename them in place, and visitors only ever see `pouchit.net`.

### How the refresh works

The workflow runs every 6 hours and on demand from the Actions tab. Each run
runs the tests, loads the saved tables from the `data` branch, runs the pipeline,
saves the updated `db/` tables and `data/presales.json` back to the `data` branch,
builds the page, and uploads `site/` to Cloudflare Pages. It never commits to
`main`, which is protected. A merge to `main` redeploys the latest saved data
without fetching. If a step fails, the previous version of the site stays live.

The `data` branch only ever holds `db/` and `data/`. Don't merge it into `main`.

Share `https://pouchit.net` (the custom domain; sign-in only works there, not on
`presalewatch.pages.dev`). Each deploy log also prints a URL like
`https://1ead08e0.presalewatch.pages.dev`, which is a frozen snapshot of that one
deploy and never updates. The page's header turns amber and says "data may be out of
date" when its data is more than 12 hours old.

GitHub often runs scheduled workflows late, so expect gaps of 6 to 8 hours between refreshes.

GitHub pauses scheduled workflows after 60 days with no repo activity. If the
site stops updating, re-enable it from the Actions tab or push any commit.

## Accounts and alerts

Signed-in users can follow artists, set a Profit % threshold for "high profit"
alerts (default 30%), turn each alert type on or off, and delete their account.
After each data refresh they get at most one digest email, and never the same
event twice.

| Path | What it does |
| --- | --- |
| `functions/api/` | The API (Cloudflare Pages Functions), deployed with the site |
| `worker/` | `presalewatch-alerts`, a cron-only Worker that emails digests hourly when data is new |
| `src/lib/` | Shared code: crypto, validation, sessions, rate limits, matching, email |
| `migrations/` | D1 schema. Tables: `users`, `preferences`, `follows`, `login_tokens`, `sessions`, `sent_alerts`, `rate_limits`, `worker_state` |
| `templates/static/` | `/alerts` (sign in and settings), `/auth/confirm`, `/unsubscribe`, `/privacy` |
| `config/security_headers.json` | Security headers used by both the static site and the API |
| `tests/js/` | API, auth, alert, and security tests (`npm test`) |

### How it works

- **Sign in:** `/alerts` asks for an email and a Turnstile check. The API emails a
  one-time link, `https://pouchit.net/auth/confirm#token=...`. Opening it shows
  "Sign in as t•••@example.com?" and a button; pressing it starts a session.
- **High profit:** Live Profit % at or above the threshold. For events without
  Live data, the midpoint of the estimated range at or above the threshold, with
  Med or High demand. Estimates are labeled as estimates in the email. Up to 25
  per digest, highest first.
- **Artists you follow:** any event whose artist matches a follow, ignoring case,
  accents, and punctuation. Each event appears once per digest.
- **The Worker** runs at :20 past every hour. It fetches `https://pouchit.net/alerts.json`,
  and if `generated_at` changed since the last complete run, it builds digests,
  records each (user, event) in `sent_alerts`, sends through Resend's batch API,
  and removes those records again if a send fails, so the next run retries. It
  sends at most `MAX_EMAILS_PER_RUN` (80) per run to stay under Resend's free
  100/day; anyone past the cap gets their digest the next hour.

Without Live resale data, an estimated event qualifies as high profit at the 30%
default only when it's rated High demand (range midpoint about +63%); the Med-demand
midpoint is about +2%. High demand needs Last.fm data. Follow alerts work regardless.

### One-time setup

Done already: the `pouchit.net` custom domain on Pages, the D1 database, the
Turnstile widget, Resend domain verification, and DMARC.

1. **Create the tables**, before the first merge, so the API never runs without them:

   ```powershell
   npx wrangler login
   npx wrangler d1 migrations apply presalewatch --remote
   ```

2. **Generate the two random secrets.** This prints a 256-bit random value. Run it
   once per secret and keep the output only long enough to paste it below:

   ```powershell
   node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"
   ```

3. **Set the Pages secrets.** Each command prompts for the value:

   ```powershell
   npx wrangler pages secret put RESEND_API_KEY --project-name presalewatch
   npx wrangler pages secret put TURNSTILE_SECRET --project-name presalewatch
   npx wrangler pages secret put UNSUBSCRIBE_SECRET --project-name presalewatch
   npx wrangler pages secret put IP_HASH_SECRET --project-name presalewatch
   ```

   `RESEND_API_KEY` is a Resend key with **Sending access** only, restricted to
   `pouchit.net`. `TURNSTILE_SECRET` is the widget's secret key (Cloudflare
   dashboard, Turnstile, your widget, Settings).

4. **Set the Worker secrets**, after the first merge has deployed the Worker (it
   starts in dry-run mode, so it sends nothing without them). `UNSUBSCRIBE_SECRET`
   must be the **same value** as on Pages, because links signed by one are
   checked by the other.

   ```powershell
   npx wrangler secret put RESEND_API_KEY --config worker/wrangler.toml
   npx wrangler secret put UNSUBSCRIBE_SECRET --config worker/wrangler.toml
   ```

5. **Create the backend deploy token.** In Cloudflare: My Profile, API Tokens,
   Create Custom Token, with only **Account, D1, Edit** and **Account, Workers
   Scripts, Edit**, limited to your account. Then:

   ```powershell
   gh secret set CLOUDFLARE_BACKEND_TOKEN
   ```

   The existing `CLOUDFLARE_API_TOKEN` stays Pages-only.

6. **Postal address.** CAN-SPAM requires a valid physical postal address (a P.O.
   box works) in every alert email. Put it in `POSTAL_ADDRESS` in
   `worker/wrangler.toml`. The Worker refuses to send real email without it.

7. **Go live.** The Worker ships with `DRY_RUN = "true"`, so it only logs what it
   would send, by user ID. Check the logs (Workers & Pages, presalewatch-alerts,
   Logs), then set `DRY_RUN = "false"` in `worker/wrangler.toml` in a pull request.

Optional: turn on the local secret-scan hook with `git config core.hooksPath .githooks`
(needs [gitleaks](https://github.com/gitleaks/gitleaks/releases)). CI runs the same scan.

### Deploys

- **Site and API:** the existing "Refresh and deploy" workflow uploads `site/` and
  `functions/` together. The API tests must pass first.
- **Worker and migrations:** "Deploy alerts backend" (`.github/workflows/backend.yml`)
  runs when `migrations/`, `worker/`, or `src/` change on `main`, or on demand.
  To add a table later, add a new numbered file to `migrations/`; never edit one
  that has already run.
- **Pull requests:** "Tests" runs the Python and JS tests, a type check, `npm audit`,
  and a gitleaks secret scan.

### Run and test locally

Needs Node.js 24.

```powershell
npm ci
npm test
npm run typecheck
python build.py
npx wrangler d1 migrations apply presalewatch --local
npx wrangler pages dev site
```

For `pages dev`, put dummy values for the four Pages secrets in a `.dev.vars`
file (Git ignores it). Sign-in won't work locally: Turnstile only accepts
`pouchit.net`, and the API only accepts requests from that origin.

To run the Worker locally in dry-run mode:

```powershell
npx wrangler dev --config worker/wrangler.toml --test-scheduled
curl "http://localhost:8787/__scheduled"
```

### Security

These protections are in place, and `tests/js/` covers them:

- **Magic links:** 256-bit random tokens, stored only as SHA-256 hashes, single
  use (atomic), expire in 15 minutes, and all of a user's other links stop
  working once one is used. The token travels in the URL fragment, so it never
  reaches server logs. Opening a link doesn't sign you in; the confirm button
  does, so email link scanners can't use it up.
- **No account enumeration:** the sign-in response is identical for any valid
  address, and the email goes out in the background.
- **Rate limits** (in D1, keyed by HMAC so no raw IPs or emails are stored): 5 per
  15 minutes and 20 per day per IP (429); 3 per 15 minutes and 10 per day per
  address (silently dropped); 60 sign-in emails per day overall. Turnstile on
  the sign-in form is verified server-side, including hostname and action.
- **Sessions:** 256-bit random IDs, stored hashed. Cookie `__Host-pw_session`,
  `HttpOnly; Secure; SameSite=Lax; Path=/`. 30-day absolute and 7-day idle
  expiry. A new session on every login; revoked on logout and account deletion.
- **Redirects:** after sign-in, only `/` or `/alerts`.
- **CSRF:** every state-changing request must carry `Origin: https://pouchit.net`
  and a JSON content type. The one exception is RFC 8058 one-click unsubscribe,
  which is authorized by its signed token and can only turn alerts off.
- **Input:** validated and length-limited on the server (email pattern with no
  CR/LF; artist names 1-100 characters with control and bidi characters
  rejected; threshold a whole number 0-500; bodies up to 4 KB). Only
  parameterized D1 queries; a test fails on string-built SQL.
- **Authorization:** every query is filtered by the session's user ID, and the
  API never accepts a user ID from the browser. Tests check that user A can't
  read, change, or delete user B's data.
- **XSS:** pages build content with `textContent` only (a test fails on
  `innerHTML` in the account scripts). Emails HTML-escape all event and user
  data, and only link to https Ticketmaster or Live Nation pages.
- **Email headers:** addresses can't contain line breaks, subjects are fixed
  text, and rendering refuses any header value with a line break.
- **Unsubscribe:** an HMAC-signed per-user token (with a per-user secret),
  checked in constant time, and honored immediately with no sign-in. Every email
  has an unsubscribe link and `List-Unsubscribe` one-click headers.
- **Headers** on every response: a strict CSP (no `unsafe-inline`; the main
  page's inline script and style are allowed by hash), HSTS, `nosniff`,
  `Referrer-Policy: no-referrer`, `frame-ancestors 'none'` and
  `X-Frame-Options: DENY`, and `Permissions-Policy`. API responses are `no-store`.
- **Worker:** cron only. No fetch handler, no `workers.dev` URL, no routes.
- **Secrets:** only in Cloudflare and GitHub secrets. gitleaks runs in CI and as
  an optional pre-commit hook. Logs go through an allowlist of fields, so emails,
  tokens, and session IDs can't be logged, and errors return a generic message.
- **Dependencies:** dev-only, pinned to exact versions with a lockfile, `npm audit`
  in CI, only `esbuild` and `workerd` allowed to run install scripts, and GitHub
  Actions pinned to commit SHAs. The runtime code has no dependencies.

### TODO

- **Contact address:** set up Cloudflare Email Routing for a `pouchit.net`
  contact address, then add it to `/privacy` (look for the TODO there) and the
  email footer.
- **Bounces and complaints (v2):** add a Resend webhook endpoint (with signature
  verification) that turns off alerts for addresses that hard-bounce or mark mail
  as spam.
- **Postal address:** fill in `POSTAL_ADDRESS` before turning off `DRY_RUN`.
