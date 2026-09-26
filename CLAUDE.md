# PresaleWatch

Read this before starting any work in this repo.

## 1. What PresaleWatch is

PresaleWatch alerts people to exclusive ticket drops and presales that are
often resold for a profit.

The current stage is an MVP: a single shareable web page that lists upcoming
US concert presales.

## 2. MVP scope

In scope:

- The presale listing page
- The core data behind it (which presales exist, when they open, and how to get access)

Out of scope for now:

- Sales, payments, signups, accounts, and emails
- Anything beyond the core page

If a request goes outside this scope, say so and check before building it.

## 3. Product rules

- **Inspiration:** OddsJam's arbitrage tool
  (https://oddsjam.com/betting-tools/arbitrage). Aim for a clean,
  data-dense table of opportunities.
- **Edge / profit %:** the edge (profit %) calculation must **not** include
  any adjustment for the probability of actually securing tickets. It
  shows the upside only.

## 4. Repo map

| Path | What it does |
| --- | --- |
| `fetch_presales.py` | Pulls upcoming US concert presales from the Ticketmaster Discovery API and saves them to `data/presales.json` |
| `build.py` | Turns `data/presales.json` into the finished page, `site/index.html`, with the data built into the page |
| `templates/index.html` | The page design: layout, styles, search, filters, and countdowns. Edit this file to change the page |
| `fixtures/sample_events.json` | A saved example of what the Ticketmaster API sends back, for reference |
| `.github/workflows/deploy.yml` | The automatic job that refreshes the data and publishes the site |
| `SPEC.md` | The original MVP product spec |
| `README.md` | Setup, local run, and deploy instructions |
| `.gitignore` | Files Git ignores, including `.env`, `data/`, and `site/` |

`data/` and `site/` are created when you run the scripts. They aren't stored
in the repo. Don't edit `site/index.html` by hand; the next build overwrites it.

**Where the data comes from:** only the Ticketmaster Discovery API. It needs a
free API key, stored as `TM_API_KEY`.

## 5. How to run

You need Python 3.11 or newer. There's nothing to install beyond that.

1. Create a file named `.env` in the project folder containing:

   ```
   TM_API_KEY=your_ticketmaster_key
   ```

   `.env` is never committed. Ask Tyler for the key or get your own at
   developer.ticketmaster.com.

2. In a terminal in the project folder, run:

   ```powershell
   python fetch_presales.py
   python build.py
   start site\index.html
   ```

   The last line opens the page in your browser (Windows). On a Mac, use
   `open site/index.html`.

To see a design change without new data, edit `templates/index.html` and run
`python build.py` again.

## 6. Hosting

- The GitHub repo is private.
- The site is hosted on Cloudflare Pages at https://presalewatch.pages.dev.
- Every merge to `main` goes live automatically. The same job also refreshes
  the data every 6 hours.

## 7. Collaboration workflow

Two people work on this repo: Tyler (owner) and his partner.

- Always `git pull` before starting work.
- **Never commit or push directly to `main`.** Treat it as protected.
  GitHub can't enforce this on a free private repo, so everyone has to
  follow it.
- Put each change on its own branch named for the change, for example
  `add-sort-by-date`, then open a pull request into `main`.
- Tyler reviews and merges every pull request.
- Write pull request descriptions in plain English: what changed, why, and
  what it looks like on the page.

## 8. Working style

- Explain changes in plain, non-technical terms.
- Make small, focused changes.
- Ask before deleting files or restructuring the project.
