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
| Category earn rates | `config/card_details.yaml` (generated from issuer pages, reviewed in PRs) | Source 1 has no per-category earn rates; source 3 can't be used by default. The card-terms pipeline re-reads 55 of the 57 cards' issuer pages; the other 2 are discontinued (not ranked). See "Card terms from issuer pages" below. |

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

The first probe (2026-09-26 22:38 UTC, 23 pages) decided the collector's weekly
cross-check: the 10 pages marked `cross_check: true` in `config/card_sources.yaml`
(Amex and Bilt pages bundle other cards' promos, so the regex cross-check skips them).
The current probe covers every card with a URL in `config/card_sources.yaml`:

<!-- probe:start -->
_Probe run: 2026-09-26 23:41 UTC. One GET per page, robots.txt respected, self-identifying User-Agent, no JavaScript._

| issuer | card | HTTP | text chars | text source | card name on page | regex fields | verdict |
|---|---|---|---|---|---|---|---|
| amex | amex-blue-business-cash | 200 | 12,138 | visible + embedded JSON | yes | annual_fee, earn_rates | usable |
| amex | amex-blue-business-plus | 200 | 13,567 | visible + embedded JSON | yes | annual_fee, earn_rates | usable |
| amex | amex-blue-cash-everyday | 200 | 10,561 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-blue-cash-preferred | 200 | 11,711 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-business-gold | 200 | 21,164 | visible + embedded JSON | yes | annual_fee, earn_rates | usable |
| amex | amex-business-platinum | 200 | 25,766 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-delta-skymiles-gold | 200 | 12,043 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-delta-skymiles-platinum | 200 | 16,459 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-delta-skymiles-reserve | 200 | 16,697 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-gold | 200 | 18,159 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-green | 200 | 80,000 | page state (fallback) | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-hilton-honors | 200 | 10,256 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-hilton-honors-aspire | 200 | 16,924 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-hilton-honors-surpass | 200 | 12,587 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-marriott-bonvoy-bevy | 200 | 13,624 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-marriott-bonvoy-brilliant | 200 | 16,786 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| amex | amex-platinum | 200 | 40,243 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| apple | apple-card | 200 | 45,924 | visible | yes | none | usable |
| barclays | barclays-jetblue-plus | 200 | 8,112 | visible | yes | bonus, annual_fee, earn_rates | usable |
| bofa | bofa-customized-cash-rewards | 200 | 21,439 | visible + embedded JSON | yes | bonus | usable |
| bofa | bofa-premium-rewards | 200 | 21,657 | visible + embedded JSON | yes | none | usable |
| bofa | bofa-premium-rewards-elite | 200 | 23,106 | visible + embedded JSON | yes | none | usable |
| bofa | bofa-travel-rewards | 200 | 20,804 | visible + embedded JSON | yes | earn_rates | usable |
| bofa | bofa-unlimited-cash-rewards | 200 | 22,613 | visible + embedded JSON | yes | earn_rates | usable |
| capital-one | capital-one-quicksilver | 200 | 6,508 | visible + embedded JSON | yes | bonus, earn_rates | usable |
| capital-one | capital-one-savor | 200 | 8,643 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| capital-one | capital-one-spark-2-cash-plus | 200 | 13,985 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| capital-one | capital-one-venture-rewards | 200 | 20,445 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| capital-one | capital-one-venture-x | 200 | 15,912 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| capital-one | capital-one-venture-x-business | 200 | 21,372 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| capital-one | capital-one-ventureone | 200 | 6,528 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| chase | chase-freedom-flex | 200 | 32,795 | visible | yes | bonus, annual_fee, earn_rates | usable |
| chase | chase-freedom-unlimited | 200 | 29,495 | visible | yes | bonus, annual_fee | usable |
| chase | chase-ihg-premier | 200 | 50,611 | visible | yes | bonus, annual_fee, earn_rates | usable |
| chase | chase-ink-business-cash | 200 | 30,208 | visible | yes | bonus, annual_fee, earn_rates | usable |
| chase | chase-ink-business-preferred | 200 | 30,113 | visible | yes | bonus, annual_fee, earn_rates | usable |
| chase | chase-ink-business-premier | 200 | 22,444 | visible | yes | bonus, annual_fee, earn_rates | usable |
| chase | chase-ink-business-unlimited | 200 | 29,477 | visible | yes | bonus, annual_fee, earn_rates | usable |
| chase | chase-marriott-bonvoy-boundless | 200 | 43,775 | visible | yes | bonus, annual_fee, earn_rates | usable |
| chase | chase-sapphire-preferred | 200 | 36,527 | visible | yes | bonus, annual_fee, earn_rates | usable |
| chase | chase-sapphire-reserve | 200 | 80,000 | visible | yes | bonus, annual_fee, earn_rates | usable |
| chase | chase-united-explorer | – | 0 | visible | no | none | blocked (request failed: ReadTimeout) |
| chase | chase-world-of-hyatt | 200 | 39,853 | visible | yes | bonus, annual_fee | usable |
| citi | citi-double-cash | 200 | 17,379 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| citi | citi-strata-elite | 200 | 17,370 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| citi | citi-strata-premier | 200 | 14,919 | visible + embedded JSON | yes | bonus, earn_rates | usable |
| discover | discover-discover-it | 200 | 11,027 | visible + embedded JSON | yes | bonus, annual_fee, earn_rates | usable |
| robinhood | robinhood-gold-card | 200 | 3,680 | visible | yes | annual_fee, earn_rates | usable |
| us-bank | us-bank-altitude-go | 200 | 12,932 | visible | yes | bonus, annual_fee, earn_rates | usable |
| us-bank | us-bank-cash | 200 | 16,949 | visible | yes | bonus, annual_fee, earn_rates | usable |
| wells-fargo | wells-fargo-active-cash | 200 | 25,666 | visible | yes | bonus, annual_fee, earn_rates | usable |
| wells-fargo | wells-fargo-autograph | 200 | 28,496 | visible | yes | bonus, annual_fee, earn_rates | usable |
| wells-fargo | wells-fargo-autograph-journey | 200 | 34,566 | visible | yes | bonus, annual_fee, earn_rates | usable |

**Aggregator feeds**

| feed | works? | entries | newest | oldest |
|---|---|---|---|---|
| https://www.doctorofcredit.com/category/credit-cards/feed/ | yes | 15 | Sat, 26 Sep 2026 01:55:03 +0000 | Sun, 20 Sep 2026 20:26:33 +0000 |
| https://www.doctorofcredit.com/feed/ | yes | 15 | Sat, 26 Sep 2026 16:30:04 +0000 | Fri, 25 Sep 2026 13:53:02 +0000 |
<!-- probe:end -->

"Text chars" is the page text the card-terms pipeline hashes and extracts from (the
probe capped it at 80,000; the pipeline now keeps up to 150,000). "Regex fields" is only
the collector's cross-check heuristic. Re-run the probe from the Actions tab (Issuer
probe → Run workflow) or locally with `python scripts/probe_issuers.py --write-findings`
(that rewrites the table between the probe markers).

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

- **Amex attribution for the cross-check.** The Amex state blob keys content by section
  ("rewardsContent", "primaryBenefitTiles"). Reading the card's own section instead of
  regexing the whole blob would make Amex bonus/fee cross-checkable. (The card-terms
  pipeline doesn't need this: it rejects extractions for the wrong card by name.)

## Card terms from issuer pages (LLM extraction, deterministic checks)

`config/card_details.yaml` is regenerated from the same issuer pages by the card-terms
pipeline (docs/ARCHITECTURE.md). Of the 57 cards in it, **55 are tracked** and **2 are
discontinued**; no card is left manual.

The first probe left 5 cards without a readable page. Each got exactly one alternative,
checked on GitHub Actions on 2026-09-27 with `scripts/probe_issuers.py --try`:

| card | first probe | one alternative tried | result |
|---|---|---|---|
| chase-amazon-prime (Prime Visa) | amazon.com: HTTP 500 | creditcards.chase.com/cash-back-credit-cards/amazon-prime-rewards | 200, 22,820 chars, name on page: **tracked** |
| chase-united-explorer | theexplorercard.com: ReadTimeout after 45 s, every probe | creditcards.chase.com/travel-credit-cards/united/united-explorer | 200, 66,024 chars: **tracked** |
| citi-aadvantage-platinum-select-world-elite | creditcards.aa.com: HTTP 403 | citi.com/credit-cards/citi-aadvantage-platinum-select-world-elite-mastercard | 200, 20,875 chars (visible + embedded JSON): **tracked** |
| citi-custom-cash | citi.com: ~700 chars | the same page's embedded data, and citi.com/credit-cards/view-all-credit-cards | the 719 characters are the whole page: "Citi is no longer accepting applications for the Citi Custom Cash Card product as of May 28, 2026 … Existing Citi Custom Cash cardmembers are not impacted." **Discontinued** |
| wells-fargo-bilt (legacy Bilt Mastercard) | none tried | none: the card was replaced by the 2026 Cardless lineup | **Discontinued** |

Discontinued cards have `override: {discontinued: true}` and a note in
`card_details.yaml` and are not in `card_sources.yaml`. The ranking and the digest's
bonus section skip them, and nobody re-extracts their terms; their hand values stay
only so a card you already hold is still valued (Custom Cash can still be reached by a
product change, per the notices linked below). Sources on the Custom Cash closure:
[citi.com](https://www.citi.com/credit-cards/citi-custom-cash-credit-card),
[NerdWallet](https://www.nerdwallet.com/credit-cards/news/citi-custom-cash-closed-to-new-applications),
[U.S. News](https://money.usnews.com/credit-cards/articles/citi-shuts-down-applications-for-popular-custom-cash-card),
[Frequent Miler](https://frequentmiler.com/citi-custom-cash-card-is-almost-certainly-being-discontinued/).

Page text is the visible text plus prose from JSON-LD and `__NEXT_DATA__`. Amex Green
shows almost no visible text (its copy lives in the page's inline state), so when visible
text is under 1,500 characters the pipeline falls back to quoted strings from that state;
normal pages never use the fallback, which keeps their hashes stable. Text length was
identical across two probes and the first smoke run, so unchanged pages hash the same.

### LLM choice

Default model: **`gpt-6-luna`** (repo variable `LLM_MODEL` to change it). As of
2026-09-26 it is OpenAI's cheapest current model with Structured Outputs: released
2026-09-22, $0.10 per 1M input tokens ($0.01 cached) and $0.50 per 1M output tokens,
available on the Responses API with a configurable reasoning effort. Alternatives if it
misbehaves: `gpt-5.6-luna` ($0.20/$1.20), `gpt-5-mini` ($0.25/$2.00), `gpt-5-nano`
($0.05/$0.40). The build sandbox couldn't reach platform.openai.com, so these facts come
from OpenAI's model and changelog pages as indexed by search plus pricing write-ups
([OpenAI: gpt-6-luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[OpenAI changelog](https://developers.openai.com/api/docs/changelog),
[OpenRouter listing](https://openrouter.ai/openai/gpt-6-luna),
[VentureBeat](https://venturebeat.com/technology/openai-releases-gpt-6-sol-and-luna-models-slashing-api-costs-50-or-more),
[Help Net Security](https://www.helpnetsecurity.com/2026/09/23/gpt-6-sol-luna-lower-api-prices/),
[MarkTechPost](https://www.marktechpost.com/2026/09/22/openai-releases-gpt-6-sol-and-luna-50-cheaper-api-pricing-and-benchmarks/),
[pricing summary](https://www.morphllm.com/openai-api-pricing)). The first live run
confirms the model name: a wrong one fails fast with "OpenAI has no model … (HTTP 404)".

Estimated usage: a page is 2k–37k input tokens (most 3k–10k) and one response of a few
thousand output tokens including reasoning, so a full forced run of 55 pages costs
roughly $0.15–0.25. Monthly runs only call the LLM for pages whose text changed (plus
RSS-queued cards), typically 5–20 calls, a few cents. Each run's job summary shows the
actual tokens and estimated cost.

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
- **An LLM in collection or scoring**: the collector and all EV math are deterministic.
  The only LLM call is the card-terms extraction, whose output is checked against the
  page and only ever lands as a PR for review.
