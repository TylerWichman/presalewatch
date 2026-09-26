# PresaleWatch

A single shareable page listing upcoming US concert presales, ranked by **edge**:
the expected resale return per dollar spent, after seller and primary fees.
See [EDGE_SPEC.md](EDGE_SPEC.md) for the edge model and [SPEC.md](SPEC.md) for the original MVP.

| File | What it does |
| --- | --- |
| `pipeline.py` | Pulls Ticketmaster, Spotify, and SeatGeek; computes edge; updates `db/*.csv`; writes `data/presales.json` |
| `calibrate.py` | Scores past predictions against real resale; `--refit` retunes the model |
| `build.py` | Renders `site/index.html`, one self-contained file with the data inlined |
| `edge.py` | The Profit % formula, demand score, and tiers (pure functions) |
| `ticketmaster.py`, `spotify.py`, `seatgeek.py` | One module per data source |
| `config/model.json` | Fees, weights, tier cutoffs, and multiple ranges |
| `config/venues.csv` | Venue capacities, entered by hand. Seeded for NYC |
| `db/`, `data/` | Generated tables and `presales.json`. Kept on the `data` branch, not `main` |

Everything uses only the Python standard library, so there is nothing to `pip install`.

## How edge works

```
Profit % = (resale × (1 − seller fee) − (face + primary fees)) ÷ (face + primary fees)
```

Fees default to 15% seller and 25% of face for primary (`config/model.json`).

- **Live (Mode A):** when SeatGeek has 10+ listings and Ticketmaster lists a face
  value, resale is the median listing, giving an exact Profit %.
- **Estimated (Mode B):** otherwise a demand score (Spotify popularity, followers per
  venue seat, 1 ÷ tour dates, NYC/LA/Chicago market) picks a High/Med/Low tier and a
  resale multiple range, shown as a margin range such as "+22% to +104%". Confidence is
  Med when all four signals are known and Low when any is missing. Missing signals are
  filled with a neutral 0.5.

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

3. Optional, for live resale and demand signals:
   - SeatGeek client ID from [seatgeek.com/account/develop](https://seatgeek.com/account/develop)
   - Spotify client ID and secret from [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)

   Without these, the pipeline still runs, but every row is a Low-confidence estimate.

4. In the project folder, create a file named `.env` containing:

   ```
   TM_API_KEY=your_consumer_key_here
   SEATGEEK_CLIENT_ID=
   SPOTIFY_CLIENT_ID=
   SPOTIFY_CLIENT_SECRET=
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
Spotify lookups are cached for 3 days per artist and SeatGeek matches for 24 hours.
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
there. To add one, copy its row into `config/venues.csv`, fill in `capacity`
(and `market_tier`: `1` for NYC, LA, or Chicago, `0.5` otherwise), and open a pull request.

## Calibration

`calibrate.py` runs after every pipeline run. Each estimated event is scored on
the SeatGeek median 7 and 14 days after its public on-sale (`db/scores.csv`), and
`db/calibration.json` reports tier accuracy, share of actuals inside the predicted
range (target 70%), and mean absolute error.

Once 50+ events are scored, the report flags a review monthly or every 25 new
scores. Then run `python calibrate.py --refit`: it fits a log-linear regression of
the actual multiple on the demand signals and rewrites the weights, tier cutoffs,
and ranges in `config/model.json`. Copy the data down first (see above), check
the printed tiers, then open a pull request with the new `config/model.json`.

To edit the page, change `templates/index.html` and rerun `python build.py`.
Don't edit `site/index.html` directly, because the build overwrites it.

## Deploy to Cloudflare Pages

GitHub Actions fetches and builds the page, then uploads `site/` to Cloudflare
Pages with Wrangler. The repo can be public or private.

One-time setup:

1. **Create a Cloudflare API token.** In the Cloudflare dashboard go to
   **My Profile → API Tokens → Create Token → Create Custom Token**, and add the
   permission **Account → Cloudflare Pages → Edit**. Copy the token.
2. **Find your account ID.** It's under **Workers & Pages** in the dashboard
   (right-hand sidebar, "Account ID").
3. **Add three repo secrets** under **Settings → Secrets and variables → Actions**:

   | Name | Value |
   | --- | --- |
   | `TM_API_KEY` | your Ticketmaster key |
   | `SEATGEEK_CLIENT_ID` | optional, enables live resale |
   | `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | optional, enables popularity and followers |
   | `CLOUDFLARE_API_TOKEN` | the token from step 1 |
   | `CLOUDFLARE_ACCOUNT_ID` | the ID from step 2 |

   Or from PowerShell with the [GitHub CLI](https://cli.github.com/), pasting each value when prompted:

   ```powershell
   gh secret set TM_API_KEY
   gh secret set SEATGEEK_CLIENT_ID
   gh secret set SPOTIFY_CLIENT_ID
   gh secret set SPOTIFY_CLIENT_SECRET
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

### How the refresh works

The workflow runs every 6 hours and on demand from the Actions tab. Each run
runs the tests, loads the saved tables from the `data` branch, runs the pipeline,
saves the updated `db/` tables and `data/presales.json` back to the `data` branch,
builds the page, and uploads `site/` to Cloudflare Pages. It never commits to
`main`, which is protected. A merge to `main` redeploys the latest saved data
without fetching. If a step fails, the previous version of the site stays live.

The `data` branch only ever holds `db/` and `data/`. Don't merge it into `main`.

Share `https://presalewatch.pages.dev`. Each deploy log also prints a URL like
`https://1ead08e0.presalewatch.pages.dev`, which is a frozen snapshot of that one
deploy and never updates. The page's header turns amber and says "data may be out of
date" when its data is more than 12 hours old.

GitHub often runs scheduled workflows late, so expect gaps of 6 to 8 hours between refreshes.

GitHub pauses scheduled workflows after 60 days with no repo activity. If the
site stops updating, re-enable it from the Actions tab or push any commit.
