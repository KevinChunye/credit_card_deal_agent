# credit_card_deal_agent

A personal agent that tracks US credit card sign-up bonuses and benefits, scores them
against **your** spending and wallet, and sends you a monthly digest by email (AgentMail)
and WhatsApp (through your OpenClaw agent on Maritime).

- **Collector** (GitHub Actions, weekly): pulls public data, normalizes it, diffs it, and
  commits snapshots to the `data` branch.
- **Agent** (OpenClaw skill on Maritime): keeps your private profile in local SQLite, does
  deterministic EV math, answers questions, and builds the digest.
- **$0/month**: Actions minutes (~1–2 min/week), AgentMail's free tier, and the Maritime
  agent you already run. No LLM calls in the pipeline.

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
WhatsApp text instead of JSON.

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

### (b) AgentMail

1. In the AgentMail console, create (or reuse) the inbox you'll forward offers to, e.g.
   `kev_work@agentmail.to`, and create an API key.
2. Set `AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX=kev_work@agentmail.to` and `OWNER_EMAIL=<your
   Gmail address>` (Maritime, step d; or your shell for local tests).
3. Check: `bin/card-agent inbox poll` → `ok: true`.

The agent only *reads* this inbox (list/get). It never labels, replies to, or forwards
messages, and it sends one kind of email: your digest, to you.

### (c) Gmail forwarding filter (including the confirmation email)

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

### (d) Install the skill on Maritime and set env vars

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

### (e) Schedule the monthly digest in OpenClaw

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

### (f) Test script: messages to send the agent

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

- `config/card_details.yaml`: curated earn rates, FX fees, protections, downgrade paths.
  Edit it when a card changes; the next collector run picks it up.
- `config/eligibility_rules.yaml`: Chase 5/24, Sapphire, Amex lifetime/family, Citi
  48-month, Capital One Venture rules, as text plus machine checks.
- `config/issuer_domains.yaml`: the inbox sender allowlist.
- `config/issuer_pages.yaml`: pages for the probe; `collect: true` ones are cross-checked weekly.
- `config/user_profile.example.yaml`: every onboarding field, documented.
- `docs/TESTING.md`: a checklist for verifying each integration with your credentials.

## Development

`pytest -q` and `ruff check . && ruff format --check .` must pass; CI runs both on every
PR, and the collector workflow runs live (without committing) on PRs that touch it.
Tests are offline: HTTP goes through `httpx.MockTransport`, AgentMail through a fake
client, and fixtures live in `tests/fixtures/`.
