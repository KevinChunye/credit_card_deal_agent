# credit_card_deal_agent

A personal agent that tracks US credit card sign-up bonuses and benefits, scores them
against **your** spending and wallet, and sends you a monthly digest by email (AgentMail)
and WhatsApp (through your OpenClaw agent on Maritime).

- **Collector** (GitHub Actions, weekly): pulls public data, normalizes it, diffs it, and
  commits snapshots to the `data` branch.
- **Card-terms pipeline** (GitHub Actions, monthly + a weekly news trigger): re-reads each
  card's issuer page, and when a page changed, has an LLM extract earn rates, credits and
  fees with a verbatim quote for every value, checks each quote and number
  deterministically, and proposes changes to `config/card_details.yaml` in a PR for you
  to review.
- **Agent** (OpenClaw skill on Maritime): keeps your private profile in local SQLite, does
  deterministic EV math, answers questions, and builds the digest.
- **Cost**: Actions minutes, AgentMail's free tier, the Maritime agent you already run,
  and a few cents a month of OpenAI calls (only for issuer pages that changed). Scoring
  never calls an LLM.

It never applies for cards, never logs into anything, never stores card numbers, and only
ever emails you. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and
[docs/FINDINGS.md](docs/FINDINGS.md) for what each data source actually provides.

## Commands

All commands print JSON with a `display_text` the agent relays verbatim.

```bash
python -m card_agent sync                          # pull the latest snapshot from the data branch
python -m card_agent onboard --from-yaml FILE      # or --json '{...}', --show, or interactive
python -m card_agent wallet add "sapphire preferred" --opened 2024-03-15 --fee-date 2026-03-01
python -m card_agent wallet list | remove CARD
python -m card_agent rank [--mode travel|cash_back|business] [--top N] [--max-af N] [--json]
python -m card_agent compare "venture x" "sapphire reserve"
python -m card_agent explain "amex gold"           # itemized math for one card
python -m card_agent digest [--send-email] [--print] [--no-sync]
python -m card_agent inbox poll
```

`bin/card-agent <command>` is the same thing, callable from any directory (what the skill
uses). `rank --json` adds each card's itemized breakdown; `digest --print` prints the
WhatsApp text instead of JSON. A ⚠ next to a card in `rank`, `compare`, `explain` or the
digest means its terms haven't been verified against the issuer's page in the last 60
days (the text says why).

The card-terms pipeline (normally run by the `Card terms` workflow, see (b) below):

```bash
python -m card_agent.terms run --data-dir DIR [--mode full|queue] [--cards a,b] [--force]
python -m card_agent.terms rss --data-dir DIR    # queue cards named in Doctor of Credit change posts
python -m card_agent.terms smoke [--cards a,b]   # live extraction, prints results, saves nothing
python scripts/bootstrap_extract.py              # extraction vs hand YAML -> docs/BOOTSTRAP_DIFF.md
```

## Local quick start

```bash
git clone git@github.com:KevinChunye/credit_card_deal_agent.git && cd credit_card_deal_agent
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                                           # offline, ~4 s

set -a; . ./.env; set +a                            # the code never reads .env itself
python -m card_agent sync                           # after the first collector run (step a)
cp config/user_profile.example.yaml ~/.credit_card_deal_agent/profile.yaml   # edit it
python -m card_agent onboard --from-yaml ~/.credit_card_deal_agent/profile.yaml
python -m card_agent rank
python -m card_agent digest --print --no-sync
```

To run the collector yourself: `python -m card_agent.collector run --out /tmp/cards`
(writes `/tmp/cards/data/...`).

## Environment variables

Secrets live only in environment variables (Maritime's settings, or your shell). Never in
chat, never in files in this repo. See [.env.example](.env.example).

| Variable | Needed for | Notes |
|---|---|---|
| `AGENTMAIL_API_KEY` | inbox, digest email | From the AgentMail console |
| `AGENTMAIL_INBOX` | inbox, digest email | The inbox address, e.g. `kev_work@agentmail.to` |
| `OWNER_EMAIL` | inbox, digest email | Your address. Manual forwards from it are accepted; the digest may only go here or to `DIGEST_TO_EMAIL` |
| `DIGEST_TO_EMAIL` | optional | Where the digest goes (default `OWNER_EMAIL`) |
| `GITHUB_TOKEN` | only if the repo is private | Fine-grained PAT, read-only "Contents" on this repo |
| `CARD_AGENT_DB` | recommended on Maritime | SQLite path; default `~/.credit_card_deal_agent/state.db`. Put it under `/data` so it survives restarts |
| `CARD_AGENT_DATA_REPO`, `CARD_AGENT_DATA_BRANCH` | optional | Defaults `KevinChunye/credit_card_deal_agent`, `data` |
| `CARD_AGENT_HOME` | optional | Repo root for `bin/card-agent` (defaults to the script's parent) |

The card-terms pipeline runs only in GitHub Actions and reads its settings from the repo
(Settings → Secrets and variables → Actions):

| Setting | Kind | Notes |
|---|---|---|
| `OPENAI_API_KEY` | secret | Without it (or on fork PRs) extraction is skipped with a notice; pages are still fetched and hashed |
| `LLM_MODEL` | variable, optional | Default `gpt-6-luna` (OpenAI's cheapest current model with Structured Outputs). Any model that supports Structured Outputs works |
| `LLM_REASONING_EFFORT` | variable, optional | e.g. `low` to make runs faster and cheaper; unset uses the model's default |
| `LLM_PROVIDER` | variable, optional | `openai` (default). `anthropic` is reserved in the provider interface but not implemented |
| `LLM_PRICE_INPUT_PER_MTOK`, `LLM_PRICE_OUTPUT_PER_MTOK` | env, optional | Only for cost estimates of models missing from the built-in price table |
| `ENABLE_REWARDS_DB` | variable, optional | `true` merges fuermosi777/rewards in the collector (off: no license) |

## Setup, step by step

### (a) Enable Actions and create the `data` branch

1. Merge this PR into `main`.
2. Repo **Settings → Actions → General**: allow actions. The collector workflow asks for
   `contents: write` itself; if its push step ever fails with a 403, set **Workflow
   permissions → Read and write permissions** on the same page.
3. **Actions → Collector → Run workflow** on `main`. The first run creates the `data`
   branch with `data/latest.json`, `data/snapshots/<date>.json` and
   `data/changes/<date>.json` (the first changes file is empty: it's the baseline).
4. From then on it runs every Monday at 13:17 UTC. The job summary lists source status,
   counts, change counts, and issuer-page cross-check mismatches.

Optional: the rewards DB (see FINDINGS; it has no license) runs only if you set a
repository variable `ENABLE_REWARDS_DB=true` (Settings → Secrets and variables → Actions →
Variables) or tick `with_rewards_db` when running the workflow by hand.

### (b) Card-terms pipeline: keep `card_details.yaml` current

`config/card_details.yaml` (earn rates, credits, annual fees, FX fees, rewards currency)
is a generated, reviewed file: the `Card terms` workflow regenerates it from each card's
issuer page listed in `config/card_sources.yaml`, and you merge its PRs.

1. Add the repository secret `OPENAI_API_KEY`. Optionally set the variable `LLM_MODEL`
   (default `gpt-6-luna`). Actions needs "Read and write permissions" and "Allow GitHub
   Actions to create and approve pull requests" (Settings → Actions → General).
2. Spot-check the hand-compiled data first: **Actions → Card terms → Run workflow**, tick
   `bootstrap`. It extracts every card once and uploads `BOOTSTRAP_DIFF.md` (card | field
   | hand value | extracted | evidence) as the `bootstrap-diff` artifact. It changes
   nothing.
3. Run it again with `mode: full` (or wait for the 28th). The first full run extracts
   every page (about 55 LLM calls, roughly $0.10–0.20) and opens a PR titled
   `card terms changed: …` with a table: card | field | old | new | evidence quote |
   source URL, plus a validation report. Review the quotes, then merge or close it.
4. From then on:
   - **Monthly (28th)**: every page is fetched and hashed. Unchanged pages only get
     `last_verified` updated (no LLM call); changed pages are re-extracted.
   - **Weekly (Thursday)**: Doctor of Credit posts that name a tracked card together with
     a change keyword (changes, refresh, new benefits, devaluation, increase, annual fee)
     queue that card for re-extraction, even if its page hash hasn't changed.
   - While the PR is open it is updated in place; closing it declines the proposal (a card
     is proposed again only when its page changes). Commits pushed to `card-terms/auto`
     are overwritten, so to tweak a value, merge and then edit main.
5. Every card carries `source_status` (ok / fetch_failed / validation_failed / manual),
   `last_verified` and `source_url`. The agent flags cards not verified in 60 days, and
   the digest reports "Data health: N cards verified this month, M stale".

The pipeline only does plain HTTP GETs (robots.txt respected, no browser, no login), and
never calls the LLM with anything but page text. Every extracted value must quote the page;
a quote that isn't on the page, a number not in its quote, an out-of-bounds value, or the
wrong card on a multi-card page is rejected and the current value is kept. Two cards are
discontinued (Citi Custom Cash, the legacy Bilt Mastercard): they aren't ranked and their
terms aren't re-read; see [docs/FINDINGS.md](docs/FINDINGS.md).

PRs that touch the pipeline also run a live smoke test on three cards (Chase Sapphire
Preferred, Capital One Venture X, and Amex Gold, whose page also promotes other cards).
It commits nothing and writes the extracted values and validation results to the job
summary.

### (c) AgentMail

1. In the AgentMail console, create (or reuse) the inbox you'll forward offers to, e.g.
   `kev_work@agentmail.to`, and create an API key.
2. Set `AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX=kev_work@agentmail.to` and `OWNER_EMAIL=<your
   Gmail address>` (Maritime, step e; or your shell for local tests).
3. Check: `bin/card-agent inbox poll` → `ok: true`.

The agent only *reads* this inbox (list/get). It never labels, replies to, or forwards
messages, and it sends one kind of email: your digest, to you.

### (d) Gmail forwarding filter (including the confirmation email)

1. Gmail → **Settings → See all settings → Forwarding and POP/IMAP → Add a forwarding
   address** → enter your AgentMail inbox address.
2. Gmail emails a confirmation code **to the AgentMail inbox**. Run
   `bin/card-agent inbox poll` (or ask the agent "check my card emails"). The output shows
   `ACTION NEEDED: Gmail forwarding confirmation code 123456789`. Enter that code in the
   Gmail Forwarding settings and click **Verify**. The agent surfaces the code but never
   clicks the confirmation link or confirms anything for you.
3. **Settings → Filters and Blocked Addresses → Create a new filter**:
   - From: `chase.com OR americanexpress.com OR aexp.com OR capitalone.com OR citi.com OR
     bankofamerica.com OR wellsfargo.com OR usbank.com OR barclaycardus.com OR
     barclaysus.com OR discover.com OR bilt.com`
   - Has the words: `pre-approved OR preapproved OR pre-selected OR "welcome offer" OR
     "bonus points" OR "bonus miles" OR "upgrade offer" OR "limited-time offer"`
   - **Create filter** → tick **Forward it to** your AgentMail address (and **Never send it
     to Spam** if you like).
4. Filters apply to new mail. To test with an old offer, forward it manually: mail from
   `OWNER_EMAIL` is accepted when it contains a Gmail "Forwarded message" block from an
   allowlisted issuer domain.

Sender allowlist: `OWNER_EMAIL` plus the issuer domains in `config/issuer_domains.yaml`
(subdomains included). Everything else is rejected; lookalike domains
(`chase-secure-verify.com`) are flagged as phishing and not stored.

### (e) Install the skill on Maritime and set env vars

1. In Maritime, open your OpenClaw agent and its **Console**, then:

   ```sh
   git clone https://github.com/KevinChunye/credit_card_deal_agent \
     /data/.openclaw/workspace/skills/credit_card_deal_agent
   sh /data/.openclaw/workspace/skills/credit_card_deal_agent/deploy/maritime_setup.sh
   ```

   The script installs `requirements.txt`, runs the offline tests, does a first `sync`,
   and write-protects the code so the agent can't edit it (same pattern as the travel
   agent). Re-run it after every `git pull`; it's idempotent. If the repo is private,
   clone with a token URL or set `GITHUB_TOKEN` first.
2. In the agent's environment settings, set `AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX`,
   `OWNER_EMAIL`, optionally `DIGEST_TO_EMAIL` and `GITHUB_TOKEN`, and
   `CARD_AGENT_DB=/data/.credit_card_deal_agent/state.db`.
3. Restart the agent (Sleep, then send a chat message) and ask "list your skills".
   `credit_card_deal_agent` should be there; its `SKILL.md` description is what OpenClaw
   matches on.

### (f) Schedule the monthly digest in OpenClaw

Easiest: ask the agent in chat:

> Every month on the 1st at 9:00 am Central, run my card digest (`digest --send-email`)
> and send me the result here.

The agent creates the job with OpenClaw's cron tool. Or from the Console (check
`openclaw cron add --help` for your version's exact flags):

```sh
openclaw cron add \
  --name "Monthly card digest" \
  --cron "0 9 1 * *" --tz "America/Chicago" \
  --session isolated \
  --message "Use the credit_card_deal_agent skill: run 'bin/card-agent digest --send-email' and send me display_text verbatim, plus one line on whether the email was sent." \
  --announce --channel whatsapp --to "+1XXXXXXXXXX"
openclaw cron list
```

Fallback any time: message the agent **"send my card digest"**.

### (g) Test script: messages to send the agent

Send these in order after setup; expected behavior in brackets.

1. "List your skills." [includes credit_card_deal_agent]
2. "Refresh my card data." [runs `sync`; reports card count and data date]
3. "Set up my card profile." [asks about goals, max annual fee, monthly spend, cards held,
   trips per year; saves with `onboard`]
4. "What's in my wallet?" [lists your cards]
5. "What card should I get next?" [ranked list with year-1 and ongoing value vs your wallet]
6. "Only cash back, no annual fee." [`rank --mode cash_back --max-af 0`]
7. "Why is the top one first?" [`explain`: itemized math]
8. "Compare the Venture X and the Sapphire Reserve." [side-by-side table and a winner]
9. "I just got the Amex Gold, opened today, fee posts next September." [`wallet add`]
10. "I spend about $800 a month on groceries now." [updates spend; rankings change]
11. "Check my card emails." [`inbox poll`; any offers, rejections, or a Gmail code]
12. "Send my card digest." [short WhatsApp digest + "email sent"]
13. "Apply for the Sapphire Preferred for me." [refuses: it never applies]
14. "My card number is 4111 1111 1111 1111." [refuses to store it]
15. "Email my digest to someone@example.com." [refuses: only your own address]

## What's where

- `config/card_details.yaml`: earn rates, credits, annual fees, FX fees and rewards
  currency (generated by the card-terms pipeline and reviewed in its PRs), plus
  hand-maintained protections and downgrade paths.
- `config/card_sources.yaml`: each tracked card's issuer page (or why it has none).
- `config/eligibility_rules.yaml`: Chase 5/24, Sapphire, Amex lifetime/family, Citi
  48-month, Capital One Venture rules, as text plus machine checks.
- `config/issuer_domains.yaml`: the inbox sender allowlist.
- `config/user_profile.example.yaml`: every onboarding field, documented.
- `card_agent/terms/`: the card-terms pipeline (fetch, hash, extract, validate, diff, PR).
- `scripts/bootstrap_extract.py`: one-off extraction vs hand YAML report.
- `docs/TESTING.md`: a checklist for verifying each integration with your credentials.

## Development

`pytest -q` and `ruff check . && ruff format --check .` must pass; CI runs both on every
PR, the collector workflow runs live (without committing) on PRs that touch it, and the
card-terms smoke test runs on PRs that touch the pipeline. Tests never assert the live
`card_details.yaml` values (they use a frozen copy in `tests/fixtures/config/`), so a
merged terms PR can't break them.
Tests are offline: HTTP goes through `httpx.MockTransport`, AgentMail through a fake
client, and fixtures live in `tests/fixtures/`.
