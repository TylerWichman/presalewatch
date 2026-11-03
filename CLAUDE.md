# PouchIt

Read this before starting any work in this repo.

## 1. What PouchIt is

PouchIt alerts people to exclusive ticket drops and presales.

The current stage is an MVP: a shareable web page that lists upcoming US
concert presales, plus free accounts with email alerts.

## 2. MVP scope

In scope:

- The presale listing page
- The core data behind it (which presales exist, when they open, and how to get access)
- Accounts and email alerts: magic-link sign-in (no passwords), following
  artists, turning alerts on or off, account deletion, and one digest email
  per data refresh

Out of scope for now:

- Profit, resale, or demand estimates (removed in November 2026; the page just lists presales)
- Sales, payments, and paid plans
- Marketing emails, and any email other than sign-in links and the alerts a user turned on
- Anything beyond the page and alerts

If a request goes outside this scope, say so and check before building it.

Security is not optional for accounts and alerts. Anything touching sign-in,
sessions, user data, or email must keep the protections listed in the README
("Accounts and alerts > Security") and their tests passing.

## 3. Product rules

- **Inspiration:** OddsJam's arbitrage tool
  (https://oddsjam.com/betting-tools/arbitrage). Aim for a clean,
  data-dense list of presales.

## 4. Repo map

| Path | What it does |
| --- | --- |
| `pipeline.py`, `ticketmaster.py` | Pulls upcoming presales from Ticketmaster and saves `data/presales.json` |
| `build.py` | Builds `site/`: the main page with data built in, `alerts.json`, the account pages, and security headers |
| `templates/index.html` | The main page design. Edit this file to change the page |
| `templates/static/` | Account pages: sign in / My alerts, sign-in confirm, unsubscribe, privacy |
| `functions/api/` | The accounts API (Cloudflare Pages Functions) |
| `worker/` | The hourly job that emails alert digests (Cloudflare Worker) |
| `src/lib/` | Code shared by the API and the Worker: auth, sessions, matching, email |
| `migrations/` | Database tables (Cloudflare D1) for accounts. Older migrations also created artist/venue tables that are no longer used |
| `tests/` | Python tests (`tests/*.py`) and API/alert tests (`tests/js/`) |
| `fixtures/sample_events.json` | A saved example of what the Ticketmaster API sends back, for reference |
| `.github/workflows/deploy.yml` | The automatic job that refreshes the data and publishes the site |
| `SPEC.md` | The original MVP product spec |
| `README.md` | Setup, local run, and deploy instructions |
| `.gitignore` | Files Git ignores, including `.env`, `data/`, and `site/` |

`data/` and `site/` are created when you run the scripts. They aren't stored
in the repo. Don't edit `site/index.html` by hand; the next build overwrites it.

**Where the data comes from:** the Ticketmaster Discovery API (`TM_API_KEY`).
User accounts live in Cloudflare D1.

## 5. How to run

You need Python 3.11 or newer for the page. The accounts API and alerts also
need Node.js 24 (`npm ci`, then `npm test`). See the README for details.

1. Create a file named `.env` in the project folder containing:

   ```
   TM_API_KEY=your_ticketmaster_key
   ```

   `.env` is never committed. Ask Tyler for the key or get your own at
   developer.ticketmaster.com.

2. In a terminal in the project folder, run:

   ```powershell
   python pipeline.py
   python build.py
   start site\index.html
   ```

   The last line opens the page in your browser (Windows). On a Mac, use
   `open site/index.html`.

To see a design change without new data, edit `templates/index.html` and run
`python build.py` again.

## 6. Hosting

- The GitHub repo is public, so anyone can read the code. Never commit API
  keys or passwords. Keys go in `.env` (which Git ignores) locally, and in
  GitHub repo secrets for the automatic job.
- The site is hosted on Cloudflare Pages at https://pouchit.net. Alert emails
  come from alerts@pouchit.net through Resend.
- The product is called **PouchIt** (it used to be PresaleWatch). The Cloudflare
  Pages project, D1 database, and alert Worker keep their original `presalewatch`
  names, because Cloudflare can't rename them in place. Visitors never see those names.
- Account and email secrets live only in Cloudflare (`wrangler secret`) and
  GitHub secrets, never in the repo, page code, or logs.
- Every merge to `main` goes live automatically. The same job also refreshes
  the data every 6 hours.

## 7. Collaboration workflow

Two people work on this repo: Tyler (owner) and his partner.

- Always `git pull` before starting work.
- **Never commit or push directly to `main`.** It's protected: changes
  reach `main` only through a pull request with one approving review.
  Only Tyler (as admin) can bypass this.
- Put each change on its own branch named for the change, for example
  `add-sort-by-date`, then open a pull request into `main`.
- Tyler reviews and merges every pull request.
- Write pull request descriptions in plain English: what changed, why, and
  what it looks like on the page.

## 8. Working style

- Explain changes in plain, non-technical terms.
- Make small, focused changes.
- Ask before deleting files or restructuring the project.
