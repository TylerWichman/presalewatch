# PouchIt

A shareable page listing upcoming US concert presales: who's playing, when each
presale opens, and how to get access. Free accounts get email alerts for artists
they follow (see [Accounts and alerts](#accounts-and-alerts)).
Live at https://pouchit.net. See [SPEC.md](SPEC.md) for the original MVP.

| File | What it does |
| --- | --- |
| `pipeline.py` | Pulls upcoming presales from Ticketmaster and writes `data/presales.json` |
| `ticketmaster.py` | The Ticketmaster Discovery API: fetching and cleaning events and presales |
| `build.py` | Renders `site/`: the main page (data inlined), `alerts.json`, account pages, and `_headers` |
| `data/` | Generated `presales.json`. Kept in the private `pouchit-data` repo, not here |

The data pipeline uses only the Python standard library, so there is nothing to `pip install`.
The accounts API and alerts are TypeScript on Cloudflare; see [Accounts and alerts](#accounts-and-alerts).

## Windows setup

1. Install **Python 3.11+** from [python.org](https://www.python.org/downloads/windows/)
   (tick "Add python.exe to PATH"), then check it in PowerShell:

   ```powershell
   python --version
   ```

2. Get a free API key at [developer.ticketmaster.com](https://developer.ticketmaster.com/)
   (My Apps → your app → **Consumer Key**).

3. In the project folder, create a file named `.env` containing:

   ```
   TM_API_KEY=your_consumer_key_here
   ```

   `.env` is in `.gitignore`, so it never gets committed. An environment variable
   with the same name works too.

## Run locally

```powershell
python pipeline.py
python build.py
start site\index.html
python -m unittest
```

A Ticketmaster fetch uses about a dozen API calls (the free tier allows ~5,000/day).
`fixtures/sample_events.json` is a saved raw API response for reference.

To build the page from the live data without fetching, copy it from the private
`TylerWichman/pouchit-data` repo first (ask Tyler for access):

```powershell
git clone --depth 1 https://github.com/TylerWichman/pouchit-data.git ..\pouchit-data
Copy-Item -Recurse -Force ..\pouchit-data\data .
```

`data/` is ignored on `main`, so this never shows up as a change.

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
   | `CLOUDFLARE_API_TOKEN` | the token from step 1 |
   | `CLOUDFLARE_ACCOUNT_ID` | the ID from step 2 |

   Or from PowerShell with the [GitHub CLI](https://cli.github.com/), pasting each value when prompted:

   ```powershell
   gh secret set TM_API_KEY
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
runs the tests, runs the pipeline, saves `data/presales.json` to the private
`pouchit-data` repo,
builds the page, and uploads `site/` to Cloudflare Pages. It never commits to
`main`, which is protected. A merge to `main` redeploys the latest saved data
without fetching. If a step fails, the previous version of the site stays live.

The saved data lives in a private repo because Ticketmaster's terms allow storing its
event data only to provide the service, not republishing it. Keep it out of this public
repo. The job reaches the private repo with the `DATA_DEPLOY_KEY` secret, a deploy key
with write access to that repo only.

Share `https://pouchit.net` (the custom domain; sign-in only works there, not on
`presalewatch.pages.dev`). Each deploy log also prints a URL like
`https://1ead08e0.presalewatch.pages.dev`, which is a frozen snapshot of that one
deploy and never updates. The page's header turns amber and says "data may be out of
date" when its data is more than 12 hours old.

GitHub often runs scheduled workflows late, so expect gaps of 6 to 8 hours between refreshes.

GitHub pauses scheduled workflows after 60 days with no repo activity. If the
site stops updating, re-enable it from the Actions tab or push any commit.

## Accounts and alerts

Signed-in users can follow artists, turn alerts on or off, and delete their account.
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
- **Artists you follow:** any event whose artist matches a follow, ignoring case,
  accents, and punctuation. Each event appears once per digest, up to 50, soonest show first.
- **The Worker** runs at :20 past every hour. It fetches `https://pouchit.net/alerts.json`,
  and if `generated_at` changed since the last complete run, it builds digests,
  records each (user, event) in `sent_alerts`, sends through Resend's batch API,
  and removes those records again if a send fails, so the next run retries. It
  sends at most `MAX_EMAILS_PER_RUN` (80) per run to stay under Resend's free
  100/day; anyone past the cap gets their digest the next hour.

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
   dashboard, Turnstile, your widget, Settings). `IP_HASH_SECRET` keys the
   rate-limit counters and the sign-in code hashes (each use is labeled, so the
   values can't be mixed up).

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
   box works) in every alert and welcome email. Put the same value in
   `POSTAL_ADDRESS` in both `worker/wrangler.toml` (alert digests) and
   `wrangler.toml` (the welcome email sent on a user's first sign-in). Neither
   sends without it: the Worker refuses to send real email, and the welcome email
   is skipped (and sent on a later sign-in instead).

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

- **Sign-in link and code:** each sign-in email carries a link and a 6-digit
  code for the same request; using either one ends both. Both expire in 15
  minutes, and all of a user's other requests stop working once one is used.
- **Links:** 256-bit random tokens, stored only as SHA-256 hashes, single use
  (atomic). The token travels in the URL fragment, so it never reaches server
  logs. Opening a link doesn't sign you in; the "Sign in as …" button does, so
  email link scanners can't use it up.
- **Codes:** 6 digits from a secure random source (uniform), stored only as a
  keyed hash (HMAC) bound to their request. A code works only in the browser that
  asked for it: that browser holds a `__Host-pw_pending` cookie (`HttpOnly;
  Secure; SameSite=Strict`, 15 minutes) whose hash is stored with the request.
  Each try is counted before the code is compared, atomically, so no more than 5
  guesses are ever checked per request, even in parallel; after 5 wrong tries the
  request is dead. Resend is allowed once every 30 seconds and ends the old code
  and link.
- **No account enumeration:** sign-in and resend responses are identical for any
  valid address, and the email goes out in the background.
- **Rate limits** (in D1, keyed by HMAC so no raw IPs or emails are stored): 5 per
  15 minutes and 20 per day per IP (429); 3 per 15 minutes and 10 per day per
  address (silently dropped); 60 sign-in emails per day overall; resends count
  against the same limits. Code tries: 20 per 15 minutes and 60 per day per IP,
  15 per 15 minutes and 50 per day per address (429). Turnstile on the sign-in
  form is verified server-side, including hostname and action.
- **Sessions:** 256-bit random IDs, stored hashed. Cookie `__Host-pw_session`,
  `HttpOnly; Secure; SameSite=Lax; Path=/`. A session ends after 90 days
  without use (each visit extends that, and the cookie) and in any case 1 year
  after sign-in. A new session ID on every sign-in; revoked at once on logout and
  account deletion.
- **Redirects:** after sign-in, only `/` or `/alerts`.
- **CSRF:** every state-changing request must carry `Origin: https://pouchit.net`
  and a JSON content type. The one exception is RFC 8058 one-click unsubscribe,
  which is authorized by its signed token and can only turn alerts off.
- **Input:** validated and length-limited on the server (email pattern with no
  CR/LF; artist names 1-100 characters with control and bidi characters
  rejected; the alert setting must be true or false; bodies up to 4 KB). Only
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
