# PresaleWatch — Profit % / Edge Feature Spec

Sep 26, 2026 · @Tyler

## Overview

Every presale row gets a Profit % (the "edge"): the expected resale return per dollar spent, net of fees on both sides. The model is OddsJam's arbitrage table, where each opportunity leads with its profit % and the table sorts by it.

Unlike sports betting, most presales have no resale market yet when they're announced. Edge is therefore a prediction until live listings appear, and the UI must say which one it's showing.

Edge = Profit %. There is no adjustment for the odds of actually getting tickets, so the table ranks by upside, not by how realistic it is to capture.

## Scope

The MVP ships a demand tier and a margin range per presale, and switches to an exact Profit % only when live resale data exists.

| In MVP | Later |
| --- | --- |
| Profit % formula with fee assumptions | Regression-based resale model |
| Face value from Ticketmaster Discovery API | Per-section / per-tier pricing |
| Live resale prices where listings exist | Buy-quantity profit calculator |
| Heuristic demand tier (High / Med / Low) + margin range | Paywall / blurred top opportunities |
| Prediction log for calibration | Alerts (email, SMS, push) |
| Sort and filter by edge | Historical edge charts per artist |

## Profit % formula

Profit % is net resale proceeds minus all-in cost, divided by all-in cost, per ticket.

```latex
\text{Profit\%} = \frac{P_{resale} \times (1 - f_{seller}) - (P_{face} + F_{primary})}{P_{face} + F_{primary}}
```

| Input | Default | Notes |
| --- | --- | --- |
| Seller fee (f\_seller) | 15% | StubHub/Vivid/SeatGeek range \~10–15%; use the conservative end |
| Primary fees (F\_primary) | 25% of face | Ticketmaster service + facility fees vary \~15–30%; use the API value when present |
| P\_resale | See Resale estimation | Median of comparable listings, not lowest |
| P\_face | See Data sources | Standard (non-platinum) ticket price |

Fees are config values, not hardcoded, so they can be tuned per platform. Median resale is used because the lowest listing is often a single outlier.

## Data sources

Face value comes from Ticketmaster, live resale from SeatGeek, and demand signals from Spotify plus event metadata. Access terms need confirming before build.

| Input | Source | Access | Risk |
| --- | --- | --- | --- |
| Face value, venue, dates, on-sale times | Ticketmaster Discovery API | Free key | Price ranges missing on some events; dynamic pricing |
| Live resale prices (low / median / avg, listing count) | SeatGeek API | Free client ID | Stats fields may be limited on the free tier |
| Live resale (secondary) | StubHub, Vivid Seats | Restricted / partner only | Likely unavailable for MVP |
| Artist popularity | Spotify Web API (followers, popularity score) | Free | Monthly listeners not exposed via API |
| Venue capacity | Manual table, seeded for NYC-area venues first | Manual | Maintenance effort |
| Tour size, added dates | Ticketmaster events count per artist | Derived | — |
| Past resale premiums | Our own prediction log (see Calibration) | Built over time | Empty at launch |

## Resale estimation

Use live listings when at least 10 exist; otherwise predict a resale multiple from demand signals and show a range, not a point.

**Mode A — Live (confidence: High).** P\_resale = median SeatGeek listing price. Refresh every 6 hours until the event date.

**Mode B — Predicted (confidence: Low/Med).** P\_resale = P\_face × multiple. The multiple comes from a heuristic demand score:

```latex
D = w_1 \cdot \text{popularity} + w_2 \cdot \frac{\text{followers}}{\text{capacity}} + w_3 \cdot \text{scarcity} + w_4 \cdot \text{market}
```

| Signal | Definition | Starting weight |
| --- | --- | --- |
| popularity | Spotify popularity score, 0–100, scaled 0–1 | 0.35 |
| followers / capacity | Spotify followers ÷ venue capacity, log-scaled 0–1 | 0.30 |
| scarcity | 1 ÷ number of dates on the tour, 0–1 | 0.20 |
| market | 1 for NYC/LA/Chicago metro, 0.5 otherwise | 0.15 |

| Demand tier | Score D | Multiple range | Displayed margin |
| --- | --- | --- | --- |
| High | ≥ 0.70 | 1.8×–3.0× | Range, e.g. +20% to +95% |
| Med | 0.45–0.70 | 1.2×–1.8× | Range |
| Low | < 0.45 | 0.7×–1.2× | Range (often negative) |

Weights and multiple ranges are placeholders to be replaced after calibration. They are guesses, and the UI labels them as estimates.

## Data model and pipeline

A scheduled Python job pulls sources, computes edge, and writes a JSON file that the static Cloudflare Pages site reads. No backend server is needed for the MVP.

| Table / file | Key fields |
| --- | --- |
| events | event\_id, artist, venue, city, event\_date, presale\_start, presale\_type, onsale\_date, face\_min, face\_max, dynamic\_pricing\_flag |
| artists | artist\_id, spotify\_popularity, spotify\_followers, tour\_date\_count |
| venues | venue\_id, capacity, market\_tier |
| resale\_snapshots | event\_id, captured\_at, listing\_count, low, median, avg |
| predictions | event\_id, predicted\_at, mode, demand\_score, tier, multiple\_low, multiple\_high, profit\_low, profit\_high |
| presales.json | One row per upcoming presale with the display fields the page needs |

1. Pull upcoming events and presales (Ticketmaster).
2. Enrich with artist and venue data.
3. Pull resale snapshots (SeatGeek) where the event exists.
4. Pick Mode A or B, compute Profit % or range, and assign a confidence label.
5. Append to predictions; write presales.json; commit to the repo, which triggers a Pages deploy.

Run it every 6 hours via GitHub Actions. Store the tables as SQLite committed to the repo, or as CSVs to start.

## UI

The table defaults to sorting by edge, descending, OddsJam-style, and always shows whether each number is live or estimated.

| Column | Content |
| --- | --- |
| Edge | Live: "+62%". Predicted: "+20% to +95%" with a High/Med/Low tier badge |
| Confidence | Live / Estimated label; hover shows the inputs |
| Event | Artist, venue, city, date |
| Presale | Type (artist, Amex, Spotify, etc.) and code source |
| Starts in | Countdown to presale start |
| Face | Face range, with a dynamic-pricing flag |
| Resale | Median resale and listing count (live only) |

Filters: minimum edge, demand tier, city, platform, and a Live-only toggle. For sorting, predicted rows use the midpoint of their range; ties go to the sooner presale.

## Calibration

Every Mode B prediction is scored against the actual median resale price 7 and 14 days after on-sale; once 50+ events are scored, a regression replaces the heuristic weights.

- **Actual multiple** = median resale at day 7 ÷ face.
- **Metrics:** tier accuracy (did High events actually resell higher?), share of actuals inside the predicted range (target ≥ 70%), and mean absolute error on the multiple.
- **Refit:** log-linear regression of the actual multiple on the demand signals, then re-derive tier cutoffs and ranges from the residuals.
- **Review cadence:** monthly, or every 25 newly scored events.

## Risks, open questions, milestones

The biggest risk is resale data access; confirm SeatGeek's terms before building Mode A.

**Risks**

- Resale APIs restrict commercial use or rate-limit heavily.
- Dynamic pricing makes face value unstable, which inflates or erases edge.
- High-edge events are usually the hardest to buy. Without a probability adjustment, top rows may overstate realistic returns.
- Early predicted ranges are uncalibrated guesses.

**Open questions**

- [ ] Does SeatGeek's free tier return median price and listing count?
- [ ] Where do presale codes and times come from? Ticketmaster only lists some presales.
- [ ] Is 15% seller / 25% primary the right default fee assumption?

**Milestones**

| # | Milestone | Done when |
| --- | --- | --- |
| 1 | Data pull | Ticketmaster + Spotify + venue table populate events/artists/venues |
| 2 | Formula + Mode B | Every row has a tier and a margin range |
| 3 | Mode A | Rows with 10+ listings show a live Profit % |
| 4 | UI | Sortable, filterable table live on Cloudflare Pages |
| 5 | Scheduler + logging | GitHub Action runs every 6h and appends predictions |
| 6 | First calibration | 50 events scored; weights refit |
