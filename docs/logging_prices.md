# Logging resale prices by hand

We don't have access to any resale API, so we check the model against resale
prices we log ourselves. These prices are only useful if they're all logged the
same way. Follow every rule below. If a rule can't be followed for an event, skip
that event rather than logging it differently.

## What to log

**The price:** the cheapest standard ticket, per ticket, **all-in with fees**,
when buying **2 tickets**.

- Set the ticket quantity filter to 2 before reading any price.
- Use the price with all fees included. If the site shows fees only at checkout,
  go as far as the checkout total and divide by 2.
- Standard tickets only. Skip VIP, Platinum, packages, parking, and listings
  marked obstructed or limited view.
- Ignore listings that can't be bought as a pair.

**The timing:** exactly **7 days** and **14 days** after the event's **general
(public) on-sale**. Use the date of the general on-sale, not the date of any
presale. Calibration accepts a price logged up to one day early or late, but aim
for the exact day.

**The source:** **StubHub**, every time. If an event has no StubHub listings,
skip it. Don't switch to another site.

## Which events to log

Pick events by a fixed rule, not by which ones look interesting. Otherwise the
model learns our guesses instead of the market.

Each week, list the events whose general on-sale was 7 days ago and split them by
venue size (capacity from `db/venues.csv`):

| Group | Capacity |
| --- | --- |
| Arenas and stadiums | 10,000 or more |
| Mid-size | 2,000 to 9,999 |
| Clubs | under 2,000 |

In each group, sort by Ticketmaster event ID and log the first 5. Log the same
events again on day 14.

About 50 events gives a first accuracy check. Plan on 100 or more before trusting
a refit.

## How to fill in the CSV

Start from `docs/observed_prices_template.csv`. One row per price:

| Column | Value |
| --- | --- |
| `event` | The Ticketmaster event ID or event page URL |
| `kind` | `resale` |
| `basis` | `ask` |
| `price_point` | `get_in` |
| `section_tier` | Leave blank |
| `standard` | `yes` |
| `price` | The per-ticket all-in price, e.g. `145.20` |
| `fees_included` | `yes` |
| `observed_on` | The date you looked, `YYYY-MM-DD` |
| `source` | `StubHub` |
| `notes` | `day 7` or `day 14`, plus anything unusual |

Calibration ignores resale rows that don't have `get_in`, `ask`, `standard yes`,
and `fees_included yes`.

Import the file with:

```
python -m ingest.import_prices prices.csv --db d1
```

The next data refresh scores the new prices. Results appear under `hand_logged`
in `db/calibration.json`:

- `rank_correlation`: how well the demand score orders events by their actual
  resale multiple, from -1 to 1. As a rough guide, above 0.3 means the score
  is picking up real demand.
- `tier_order_holds`: whether High events resold higher than Med, and Med higher
  than Low.
