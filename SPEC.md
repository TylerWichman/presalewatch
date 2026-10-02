# PouchIt — MVP Spec (Product Demo)

## 1. Goal
A single shareable web page showing **upcoming concert presales**: who, when it opens, and how to get access. The purpose is to show the product to a friend and see whether the data is useful. It has no accounts, no alerts, and no payments.

## 2. What the page does
- Lists upcoming US concert presales, sorted by presale start time (soonest first)
- Each card shows the artist, event name, venue, city/state, and event date. It also shows the presale name (e.g., "Artist Presale," "Citi Cardmember"), the presale open/close times in ET, and the public on-sale time. A **"Get Tickets"** link goes to the official Ticketmaster page.
- Countdown badge: "Opens in 3h 12m" / "Live now" / "Ended"
- Search box to filter by artist, city, or venue
- Filters: Opening today · This week · All; state dropdown
- A "Last updated" timestamp at the top
- Mobile-friendly and clean enough to share as-is

## 3. Out of scope
Signups, logins, emails/alerts, backend server, database, payments, non-Ticketmaster sources, and any purchasing or checkout automation.

## 4. Data source
**Ticketmaster Discovery API v2** (free key: developer.ticketmaster.com)
- `GET /discovery/v2/events.json` with `countryCode=US` and `classificationName=music`, sorted by date and paginated
- Presales: `event.sales.presales[]` (`name`, `description`, `startDateTime`, `endDateTime`, `url`); public on-sale: `event.sales.public`
- Keep only events with at least one presale ending in the future
- Respect limits (5 req/sec, ~5k/day), back off on 429
- Verify field names against a live response before building

## 5. Architecture
- **`fetch_presales.py`**: Python 3.11 script that calls the API and writes a clean `data/presales.json`
- **`build.py`**: renders `site/index.html` with the data embedded inline (a single self-contained file, vanilla JS for search, filters, and countdowns; no frameworks)
- **Hosting**: GitHub Pages from the `site/` folder, giving a shareable link
- **Refresh**: a GitHub Actions workflow runs fetch + build every 6 hours and commits the updated page. The API key is stored as a repo secret, never in the page.
- **Dev OS**: Windows, so use `python -m` / `python script.py` commands only

## 6. Success check
- The page loads on phone and desktop from a public link
- 50+ upcoming presales are shown, with correct times (spot-check 10 against Ticketmaster)
- Your friend can find a presale for an artist they care about in under 30 seconds

## 7. Milestones
1. Script that pulls presales and saves a sample fixture plus `presales.json`
2. Static page that renders from the JSON, with search, filters, and countdowns
3. GitHub Pages deploy and a scheduled Actions refresh
4. README covering local run and deploy steps

## 8. Next (after the friend demo)
Follow artists, email alerts, and more presale sources (AXS, fan clubs, card programs).
