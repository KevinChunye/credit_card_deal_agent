# Findings: data sources and easy paths

Everything here was checked on 2026-09-26. The build sandbox could only reach
`raw.githubusercontent.com` and PyPI (its egress policy blocked Doctor of
Credit and every issuer site), so the issuer probe and the feed checks ran on
**GitHub Actions runners** via `.github/workflows/probe.yml`. That is the same
network the weekly collector uses, so the results reflect production.

## TL;DR: what's wired, what isn't

| Source | Status | Why |
|---|---|---|
| 1. andenacitelli/credit-card-bonuses-api | **Wired** (required) | One static JSON GET, MIT licensed, 175 cards with bonuses, fees, credits and historical offers. |
| 2. Doctor of Credit RSS | **Wired** | The credit-card category feed works with a plain GET; ~15 posts/page, paginated. |
| 3. fuermosi777/rewards | **Built, OFF by default** | Best data for earn rates/benefits/protections, but the repo has **no license**, so no reuse is granted. You decide; see below. |
| 4. Issuer pages | **10 pages wired as a cross-check** | 21 of 23 pages are readable with one plain GET; 10 state the bonus/fee unambiguously enough to compare. Nothing needed a browser, a login, or anti-bot work. |
| Category earn rates | `config/card_details.yaml` (hand-curated) | Source 1 has no per-category earn rates; source 3 can't be used by default. A static file of ~57 popular cards is the simplest thing that works. |

## Source 1: credit-card-bonuses-api (MIT)

- `GET https://raw.githubusercontent.com/andenacitelli/credit-card-bonuses-api/main/exports/data.json`
  returns a JSON **array** of 175 cards (~125 KB). Fetched once per collector run.
- Also exported as `data.csv` and `data.yaml` (same repo, `exports/`), if that's ever handier.
- Real schema (verified against `src/api.yaml` and the export): `cardId, name, issuer,
  network, currency, isBusiness, annualFee, isAnnualFeeWaived, universalCashbackPercent,
  url, imageUrl, credits[], offers[], historicalOffers[], discontinued`, plus optional
  `countsTowards524` and `details`. Offers: `spend, amount[{amount, currency?}], days,
  credits[]`, optional `expiration, isPublic, details, url, referralUrl`.
- **Gotchas found by running it on real data** (both fixed, with tests):
  - Credits have an optional `currency`, and it isn't always USD: "10k award flight
    discount" is 10,000 **United miles**, Southwest/Wyndham "Anniversary Points" are
    points. Read as dollars, United Explorer came out at +$5,000/yr. Now converted with
    your own cpp. One quirk: "$60 Hilton credit per quarter" is tagged `HILTON` but is
    dollars, so a `$` in the description wins.
  - Offer `credits` can be a free-night certificate in the card's currency (Marriott
    Bold: "1x FNC, <= 50k" = 50,000 **Marriott points**, not $50,000). Stored as
    `extra_points` on the offer.
  - Two different Wells Fargo cards are both named "Expedia One Key"; ids are
    disambiguated deterministically (lower fee keeps the plain id).
  - `spend: 0.01` means "any purchase"; normalized to 0.
- **Not in this source**: per-category earn rates, foreign transaction fees, travel
  protections. `universalCashbackPercent` is only a flat base rate.
- **Easy win**: `historicalOffers` lists the best recent offers per card, which gives
  "is this offer elevated / what's the historical high" from day one, before our own
  weekly snapshots accumulate history.

## Source 2: Doctor of Credit RSS

- `https://www.doctorofcredit.com/category/credit-cards/feed/` is the **credit-card-only
  category feed** (WordPress). Much better than the main feed:

  | feed | entries | span on 2026-09-26 |
  |---|---|---|
  | `/category/credit-cards/feed/` | 15 | ~6 days (Sep 20–26) |
  | `/feed/` (everything) | 15 | ~1 day, mostly bank-account bonuses |

- Pagination works WordPress-style: `...?paged=2`. The collector keeps a rolling 35-day
  window by merging each run's posts with the previous snapshot's, so a weekly run needs
  1–2 requests (the first run up to 6).
- Post `<category>` tags carry issuer and card names ("chase southwest", "venmo"), useful
  context alongside the title matcher.
- Classification is rule-based (amount + bonus words in the title; "elevated", "is back",
  "increased" etc. mark elevated). Unmatched posts are kept as `news`. Links are stored,
  never followed.

## Source 3: fuermosi777/rewards (NO LICENSE: off by default)

- Static JSON, one file per card (`data/cards/<id>.json`, `data/bonuses/<id>.json`),
  188 cards. Rich: `earningRates[]` with categories and caps, `benefits[]` with
  statement-credit amounts and renewal periods, `foreignTransactionFee`, insurance
  (primary/secondary rental, trip delay, cell phone...). Records are marked
  `updatedBy: agent`, so spot-check anything important.
- **License: none.** The repository has no LICENSE file (checked `LICENSE`,
  `LICENSE.md`, `license`: all 404). Without a license, no permission to reuse or
  redistribute is granted, and the collector commits snapshots to your repo. So:
  - The adapter (`card_agent/collector/rewards_db.py`) is built and tested, but runs only
    if you opt in: the Collector workflow's `with_rewards_db` input, or a repository
    variable `ENABLE_REWARDS_DB=true`.
  - When enabled it does **one** `git clone --depth 1` per run and only fills gaps:
    earn rates for cards not in `card_details.yaml`, unknown FX fees, protections,
    and benefit kinds the bonuses API didn't give that card.
  - Suggestion: open an issue asking the author to add a license (MIT would match
    source 1). If they do, flip the variable.
- Its ids map to ours as issuer slug + name slug (`american-express-gold` → `amex-gold`).

## Source 4: issuer pages (easy mode only)

`scripts/probe_issuers.py`: one GET per page, robots.txt respected, ≥2 s between
requests to the same host, **honest User-Agent**
(`Mozilla/5.0 (compatible; credit-card-deal-agent/0.1; +repo URL)`). We deliberately
don't impersonate a desktop browser; nothing was blocked anyway.

<!-- probe:start -->
_Probe run: 2026-09-26 22:38 UTC on a GitHub Actions runner. One GET per page, robots.txt respected, self-identifying User-Agent, no JavaScript._

| issuer | card | method that works | fields available | blocked/JS-only? | wired? |
|---|---|---|---|---|---|
| amex | amex-blue-cash-preferred | inline_state, static_text | bonus, annual_fee, earn_rates | no | no: page state bundles other cards' promos |
| amex | amex-gold | inline_state, static_text | bonus, annual_fee, earn_rates | no | no: same (a Delta "80,000 miles" promo and the Blue Cash fee appear on the Gold page) |
| amex | amex-platinum | inline_state, static_text | bonus, annual_fee, earn_rates | no | no: same |
| apple | apple-card | none | none | no (readable; rates not phrased as matchable text) | no |
| barclays | barclays-aadvantage-aviator-red-world-elite | none | none | HTTP 404 (URL moved) | no |
| barclays | barclays-jetblue-plus | static_text | bonus, annual_fee, earn_rates | no | **yes** |
| bilt | wells-fargo-bilt | static_text | bonus, annual_fee, earn_rates | no | no: one page lists the whole 2026 lineup |
| bofa | bofa-customized-cash-rewards | json_ld | bonus (weak) | no | no |
| bofa | bofa-premium-rewards | none | none | no (readable; offer text not matched) | no |
| capital-one | capital-one-quicksilver | static_text | bonus (weak), earn_rates | no | no |
| capital-one | capital-one-savor | static_text | bonus, annual_fee, earn_rates | no | **yes** |
| capital-one | capital-one-venture-x | static_text | annual_fee, earn_rates (bonus hit was a referral blurb) | no | **yes** (fee) |
| chase | chase-freedom-unlimited | static_text | bonus, annual_fee | no | **yes** |
| chase | chase-ink-business-preferred | static_text | bonus, annual_fee, earn_rates | no | **yes** |
| chase | chase-sapphire-preferred | static_text | bonus, annual_fee, earn_rates | no | **yes** |
| citi | citi-double-cash | json_ld, static_text | bonus (weak), annual_fee, earn_rates | no | no |
| citi | citi-strata-premier | json_ld, static_text | earn_rates | no | no |
| discover | discover-discover-it | static_text | annual_fee, earn_rates (bonus hit was Cashback Match copy) | no | no |
| robinhood | robinhood-gold-card | static_text | annual_fee, earn_rates | no | no (no bonus; "no annual fee" but requires Gold membership) |
| us-bank | us-bank-altitude-go | static_text | bonus, annual_fee, earn_rates | no | **yes** |
| us-bank | us-bank-cash | static_text | bonus, annual_fee, earn_rates | no | **yes** |
| wells-fargo | wells-fargo-active-cash | static_text | bonus, annual_fee, earn_rates | no | **yes** |
| wells-fargo | wells-fargo-autograph | static_text | bonus, annual_fee, earn_rates | no | **yes** |

**Aggregator feeds**

| feed | works? | entries | newest | oldest |
|---|---|---|---|---|
| https://www.doctorofcredit.com/category/credit-cards/feed/ | yes | 15 | Sat, 26 Sep 2026 01:55:03 +0000 | Sun, 20 Sep 2026 20:26:33 +0000 |
| https://www.doctorofcredit.com/feed/ | yes | 15 | Sat, 26 Sep 2026 16:30:04 +0000 | Fri, 25 Sep 2026 13:53:02 +0000 |
<!-- probe:end -->

"Weak" means only a bare amount matched (e.g. "minimum transfer is 1,000 points"),
not an offer sentence ("Earn 75,000 points after you spend $5,000…"); the cross-check
ignores weak hits. Re-run the probe any time from the Actions tab (Issuer probe → Run
workflow) or locally with `python scripts/probe_issuers.py --write-findings`
(that rewrites the table between the probe markers, without the "wired?" column).

### What the issuer pages told us on day one

- **Plain GET is enough almost everywhere.** No page was blocked by bot protection,
  none required JavaScript to show its offer text (Barclays' Aviator Red URL just
  moved). Amex serves everything server-side in a `window.__…__` state blob; Citi and
  BofA add schema.org JSON-LD (`CreditCard`, `FAQPage`).
- **Real discrepancies with source 1** (exactly what the cross-check is for):
  - Wells Fargo Active Cash page: **$100** bonus after $500; API: $200.
  - Barclays JetBlue Plus page: **70,000** points; API: 75,000.
  - Chase Freedom Unlimited page: **$200** (20,000 points) after $500; API: 25,000 points.
  - Capital One Venture X page: "up to 100,000 miles" is the *referral* program, not the
    public offer (API: 75,000). The cross-check ignores it.
- **Earn-rate changes the seed must track**: chase.com shows Sapphire Preferred now earns
  3x at gas stations and EV charging (seed updated). biltrewards.com/card shows the 2026
  lineup (Bilt Blue $0 with $100 Bilt Cash; Obsidian $95; Palladium $495 with 50,000
  points + Gold status after $4,000; up to 1.25x on rent/mortgage) issued by Cardless; the
  bonuses API still has only the legacy Wells Fargo Bilt card.

### Possible next easy paths (not built)

- **Issuer earn-rate text as a seed check.** The probe already extracts lines like
  "10X Miles on hotels & rental cars booked through Capital One Travel". A weekly diff of
  that text against `config/card_details.yaml` would flag stale seed entries. Cheap, but
  it's more regex surface to maintain, so I left it as a follow-up.
- **Amex attribution.** The Amex state blob keys content by section ("rewardsContent",
  "primaryBenefitTiles"). Reading the card's own section instead of regexing the whole
  blob would likely make Amex cross-checkable. Worth it only if Amex numbers in the API
  turn out to drift.

## What I skipped, and why

- **Headless browsers, JS rendering, CAPTCHA/anti-bot handling, logins**: never needed and
  never built, per the ground rules.
- **Pages that were readable but not attributable** (Amex, Bilt's multi-card page) are
  probed but not wired.
- **Apple Card and Robinhood Gold** aren't in source 1; they're defined in
  `config/card_details.yaml` (Robinhood's $50/yr Gold membership is counted as its annual
  fee).
- **Bilt 2.0 cards** aren't modeled: the API doesn't have them yet and their earn
  structure (Bilt Cash, housing multipliers) doesn't fit a flat category table without
  guessing.
- **Co-brand bonus categories** ("6x at Marriott", "2x on United") are intentionally not
  in the seed: they don't map to general spend categories, so the scorer undercounts
  co-brand cards rather than overcounts them.
- **An LLM** anywhere in collection, extraction or scoring: everything is deterministic.
