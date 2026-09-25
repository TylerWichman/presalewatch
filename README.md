# PresaleWatch

A single shareable page listing upcoming US concert presales from the
Ticketmaster Discovery API: who, when each presale opens, and how to get access.
See [SPEC.md](SPEC.md) for the product scope.

- `fetch_presales.py` pulls presales and writes `data/presales.json`
- `build.py` renders `site/index.html`, one self-contained file with the data inlined
- `.github/workflows/deploy.yml` refreshes the data every 6 hours and deploys to Cloudflare Pages

Both scripts use only the Python standard library, so there is nothing to `pip install`.

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

   `.env` is in `.gitignore`, so it never gets committed. You can set the
   `TM_API_KEY` environment variable instead if you prefer.

## Run locally

```powershell
python fetch_presales.py
python build.py
start site\index.html
```

A fetch uses about a dozen API calls (the free tier allows ~5,000/day).
`fixtures/sample_events.json` is a saved raw API response for reference.

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

### How the refresh works

The workflow runs every 6 hours, on every push to `main`, and on demand from
the Actions tab. Each run fetches fresh data, builds the page, and uploads
`site/` to Cloudflare Pages. Nothing is committed back to the repo. If a fetch fails, the
run fails and the previous version of the site stays live.

GitHub pauses scheduled workflows after 60 days with no repo activity. If the
site stops updating, re-enable it from the Actions tab or push any commit.
