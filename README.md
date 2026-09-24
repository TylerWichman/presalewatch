# PresaleWatch

A single shareable page listing upcoming US concert presales from the
Ticketmaster Discovery API: who, when each presale opens, and how to get access.
See [SPEC.md](SPEC.md) for the product scope.

- `fetch_presales.py` pulls presales and writes `data/presales.json`
- `build.py` renders `site/index.html`, one self-contained file with the data inlined
- `.github/workflows/deploy.yml` refreshes the data every 6 hours and deploys to GitHub Pages

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

## Deploy to GitHub Pages

One-time setup:

1. Create a **public** GitHub repo and push this project to the `main` branch.
   (GitHub Pages on a free account requires a public repo. The API key is
   never in the code or the page, so this is safe.)
2. **Settings → Secrets and variables → Actions → New repository secret**:
   name `TM_API_KEY`, value = your key.
3. **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. **Actions → Refresh and deploy → Run workflow**.

When it finishes, the site is at `https://<your-username>.github.io/<repo-name>/`
(the link is also shown on the workflow run).

Or do all of it from PowerShell with the [GitHub CLI](https://cli.github.com/)
after `gh auth login`:

```powershell
gh repo create presalewatch --public --source . --push
gh secret set TM_API_KEY          # paste your key when prompted
gh api -X POST "repos/{owner}/{repo}/pages" -f build_type=workflow
gh workflow run deploy.yml
```

### How the refresh works

The workflow runs every 6 hours, on every push to `main`, and on demand from
the Actions tab. Each run fetches fresh data, builds the page, and publishes
`site/` to Pages. Nothing is committed back to the repo. If a fetch fails, the
run fails and the previous version of the site stays live.

GitHub pauses scheduled workflows after 60 days with no repo activity. If the
site stops updating, re-enable it from the Actions tab or push any commit.
