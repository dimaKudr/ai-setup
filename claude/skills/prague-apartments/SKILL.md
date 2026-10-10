---
name: prague-apartments
description: Analyze a list of Prague 2kk apartment listing URLs (sreality.cz, bezrealitky.cz, or other) for a buy-to-let-then-sell-in-5-years plan, and republish the results to the existing comparison artifact. Use when the user pastes apartment listing links and asks for analysis, ranking, or to update "the report"/"the artifact".
---

# Prague apartments buy-to-let analysis

Turns a pasted list of listing URLs into normalized data, runs it through the
user's fixed cash-flow methodology, and republishes the results to the
existing live artifact — replacing its contents with only the apartments
from the current list (do not keep old listings unless the user says to
merge instead of replace).

## Step 1 — scrape

Run the bundled scraper on every URL the user pasted:

```
python3 .claude/skills/prague-apartments/scripts/scrape_listing.py <url1> <url2> ...
```

(or `--file urls.txt` if there are many). It handles sreality.cz and
bezrealitky.cz with structured extraction; any other domain falls back to a
best-effort regex scrape of price/m² and is flagged `"_note"` for manual
verification — always sanity-check generic-fallback records against the page
before using them (open the URL / fetch it yourself if a field looks off).

If a script dependency is missing (`urllib` only — stdlib, no install
needed), it will just work with the system `python3`.

Each result has `error` set if the fetch or parse failed (e.g. listing taken
down, reserved, site changed structure) — surface these to the user rather
than silently dropping the listing.

**Garage/parking inclusion check — never trust the boolean `garage` field
alone.** A site's "garage: true" / a non-empty `parkingPlaces` array only
means a parking spot *exists in the building/project* — it says nothing
about whether it's bundled into the listed price. Sellers commonly price it
as a separate line item (e.g. "Parkovací místo v suterénu domu 750 000,-",
or central-group.cz's `parkingPlaces[].totalPriceWithVAT` being distinct
from the apartment's own price). The scraper's `garage_in_price` field
(`true` / `false` / `null`) and `garage_extra_cost_czk` are derived from the
actual description text (Czech inclusion phrases like "součástí ceny",
"již v ceně", "náleží k jednotce" vs. a standalone price near a parking
mention) — use these, not the bare `garage` boolean, when writing "gараж у
ціні" in any pick card or table row:
- `garage_in_price: true` → safe to say "гараж/паркінг у ціні" with the
  evidence sentence to back it up if asked.
- `garage_in_price: false` with `garage_extra_cost_czk` set → state the
  extra cost explicitly ("паркінг +N Kč окремо, не в ціні").
- `garage_in_price: null` → genuinely ambiguous (mentioned as a feature but
  no explicit inclusion/price statement found) — say so, don't guess either
  way.
This same check applies to `cellar_in_price`/`cellar_extra_cost_czk` on
central-group.cz records (storage units are also sold as separate priced
items there).

## Step 2 — apply methodology

Apply the exact rules from the `prague-apartment-methodology` memory (check
your memory system for it; if unavailable, ask the user or reconstruct from
the live artifact's existing rows) to each scraped record:

- Kitchen/furniture capex: if `has_kitchen_or_furniture` is `false` or
  `null` (unknown — treat as "not confirmed" and add capex, same as "no"),
  add 500,000 Kč to price before cost-based calcs. If `true`, add 0.
- Mortgage: 20% down, 80% financed, 4.79%/yr, 28-year annuity. Monthly
  payment ≈ loan × 0.005410 (recompute only if the user changes these
  terms).
- Кешфлоу = monthly mortgage payment − district rent estimate (see the
  memory's district rent table; if a district isn't in the table, ask the
  user for an estimate or flag it rather than guessing).
- Price/m² shown rounded to nearest thousand with "К" suffix; price in
  millions with "М" suffix.
- "Район" and "Зростання" are judgment calls (metro proximity,
  gentrification, construction risk) — not derived from scraped fields.
- Carry forward any per-listing caveats already documented in memory
  (reserved status, mislabeled room counts, etc.) if the same listing
  reappears in the new list.

## Step 3 — update the artifact

Fetch the current artifact (`prague-apartment-report` memory has the URL) via
`Artifact` with `action: "read"`, then republish with `url:` set to that same
link so it updates in place. The table should contain **only** the apartments
from the user's current list — drop everything else that was previously in
the table. Keep the existing structure (top-picks cards, sortable
`<main-table>`, caveats section) unless the user asks to restructure it.

## Step 4 — persist what changed

If the district rent table, mortgage terms, or any other assumption changed
during this run, update the `prague-apartment-methodology` memory so future
runs stay consistent. If the artifact URL changed (new artifact created
instead of updating in place — shouldn't happen, but verify), update the
`prague-apartment-report` memory too.
