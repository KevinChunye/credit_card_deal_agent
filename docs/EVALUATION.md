# Agent evaluation: tools, memory, loop, team, recovery

The question this page answers: **can the agent use tools and memory,
coordinate a small team, evaluate what happens, and recover when something
goes wrong?** Each section says where the behavior lives in the code, how to
see it in a chat, and what evidence backs it (tests and a baseline vs
improved run).

"The agent" here is the OpenClaw chat model on Maritime following
[SKILL.md](../SKILL.md), plus the deterministic tools it calls
(`bin/card-agent <command>`). The chat model handles language; the tools
handle every number, check and memory write.

| Criterion | Where | See it in chat | Evidence |
|---|---|---|---|
| 1. Tool use | `card_agent/cli.py`: 17 commands | "What card should I get?" | `tests/test_cli.py`, `tests/test_agent.py` |
| 2. Memory | `card_agent/store.py` (SQLite) | "Not interested in X", then "What do you remember about me?" | `test_hidden_cards_stay_out_and_memory_says_why`, scenario S5 |
| 3. Observable loop | `card_agent/advisor.py`, the `next` field, `trace.jsonl` | "How did you decide?" | `test_advise_runs_the_loop_and_stops_on_a_checked_pick`, trace tests |
| 4. Team | `card_agent/verifier.py` (Verifier subagent) | "Should I get X?" | `test_verify_hands_over_a_bounded_brief`, scenario S2 |
| 5. Recovery | `snapshot.refresh`, `matching.suggest`, the Advisor's plan revision | Outage demo below | `tests/test_recovery.py`, scenarios S3 and S4 |
| 6. Evaluation | `scripts/eval_agent.py` | none | [results](#6-evaluation-baseline-vs-improved) |

## 1. Tools

Every tool is a subcommand that prints JSON with `display_text` (ready for
chat) and `next` (the loop instruction, see section 3).

| Kind | Commands | What it does |
|---|---|---|
| Retrieve | `sync`, `inbox poll` | Downloads the weekly card snapshot (177 cards, offers, news); reads issuer offers forwarded to the AgentMail inbox |
| Decide | `advise`, `rank`, `explain`, `compare`, `verify`, `use`, `credit` | Runs the scorer, the Verifier and issuer rules; picks the card to use for a purchase; runs the credit-health check |
| Act on memory | `onboard`, `wallet`, `hide`, `unhide` | Saves the profile, spending, cards held and preferences |
| Act outward | `digest --send-email` | Emails the digest, only ever to `OWNER_EMAIL` / `DIGEST_TO_EMAIL` |
| Look back | `memory`, `trace` | What is stored; what the agent did and why |
| Hand off | `apply-link` | The issuer's own application page and pre-approval page. It never applies. |

The tools are what make the answers right. The chat model never computes a
number; it relays the tools' text.

## 2. Persistent memory

Memory is a private SQLite database at `$CARD_AGENT_DB`. On Maritime that is
`/data/.credit_card_deal_agent/state.db`, which survives restarts,
new chats and channel changes (web chat, WhatsApp).

| Stored | Written by | Read by | How it changes behavior |
|---|---|---|---|
| Profile: goals, fee limit, trips, score range, credit limit | `onboard` | every command | Fee limit filters cards; goals weight year 1 vs later; score range changes the credit tips and the Verifier's credit-fit check |
| Monthly spending by category | `onboard` | `advise`, `rank`, `explain`, `use`, `credit` | Earn value and whether a minimum spend is reachable |
| Wallet with dates | `wallet`, `onboard` | all scoring, issuer rules, `credit` | Only what a card adds to your wallet counts; issuer rules like Chase 5/24 |
| Point values, credit usage | `onboard` | scoring | The cents-per-point and usage behind every dollar figure |
| Hidden cards and issuers, with reason and date | `hide`, `unhide` | `advise`, `rank`, the digest | Hidden cards never come back ("🙈 Skipping 1 card…") |
| Past picks | `advise` | `advise`, `memory` | "📌 Same pick as when you asked on Sep 20" or "🔄 Changed since…" |
| Offers from your inbox, processed message ids | `inbox poll` | digest | No email is processed twice |
| Digests sent | `digest` | `memory` | History of what went out |
| Trace log (`trace.jsonl`) | every command | `trace` | Lets the person replay the agent's decisions |

`memory` renders all of it in plain words. Scenario S5 below measures the
effect: without memory, a card the person rejected comes back in the next
session.

## 3. The observable loop

`advise` ("what card should I get?") runs an explicit loop in
`card_agent/advisor.py`. Every step is recorded, and `trace` replays it.
This is a real run on the live data for someone spending about $1,000 a
month:

```
🕐 Sep 27 19:18 · advise --no-sync · 129 ms · done
   🎯 Find the best card for you to apply for next, annual fee up to $400.
   🤔 Check what I remember about you first.
   🛠️ Read your spending, wallet and hidden cards from memory.
   👀 $1,000/mo of spending on file, 3 open cards, 0 hidden.
   ⚖️ Enough to rank cards, so continue.
   🤔 Check how fresh the saved card data is.
   👀 Card data is 1 day old: fresh enough.
   🤔 Rank every card against your wallet and spending.
   🛠️ Score all cards with the deterministic scorer.
   👀 105 cards fit your filters.
   ⚖️ Before recommending the top card, get it checked.
   🕵️ Ask the Verifier to check candidate #1, Chase Marriott Bonvoy Boundless.
   📋 Verifier on Chase Marriott Bonvoy Boundless: fail (✅ 6 passed · ⚠️ 1 caveat · ❌ 1 failed).
   ⚖️ Reject it (Needs $4,000 in 120 days, but your usual spending is ~$3,943. …), try the next card.
   …candidates #2 to #5 fail the same way…
   🤔 Most candidates failed on minimum spend, so revise the plan: consider only cards whose minimum spend fits your usual spending.
   🛠️ Re-filter the ranking by reachable minimum spend.
   👀 67 cards fit; checking the top 5.
   🕵️ Ask the Verifier to check candidate #6, Citi AAdvantage Platinum Select World Elite.
   📋 Verifier on Citi AAdvantage Platinum Select World Elite: warn (✅ 7 passed · ⚠️ 1 caveat).
   ⚖️ Accept Citi AAdvantage Platinum Select World Elite, with 1 caveat.
   🛠️ Save this pick to memory for next time.
   🛑 Stop: a checked pick is ready.
```

**Stopping conditions** (in code): stop when a candidate passes, or is
accepted with caveats; stop and ask after the checks run out; `MAX_STEPS`
(60) guards against a runaway loop. In SKILL.md the chat model also stops
after 4 commands per message, and after the same command fails twice.

**Ask-a-person conditions** (in code, returned as `next.action = ask_user`):

1. no spending profile yet: it asks five plain questions;
2. no card data and it can't be downloaded;
3. nothing fits the filters: raise the fee limit, or include business or hidden cards?;
4. nothing passes the Verifier, even after revising the plan;
5. an ambiguous or unknown card name: "Did you mean …?".

**The `next` contract** turns every tool result into a loop decision for the
chat model: `stop`, `ask_user`, `run` (a recovery step such as `sync`, then
retry once), or `fix_command` (the model's own command was malformed: fix it
quietly and retry once). The rules are in SKILL.md under "How you work".

**Logs**: each command appends one JSON line to `trace.jsonl` next to the
database. Each line holds the command, its safe arguments, ok, duration,
`next`, a summary, and for `advise` and `verify` every step, including the
exact brief handed to the Verifier and its report. The log sits next to the
database and holds nothing the database doesn't; even so, the raw JSON passed
to `onboard` and `hide` reasons are kept out of it. `trace` shows the chat
model only the step texts; briefs and reports stay in the file.

## 4. A small team: the Advisor and the Verifier

The Advisor (main loop) delegates checking to the **Verifier**
(`card_agent/verifier.py`):

- **Role**: an independent second look at one proposed card. It returns
  pass, warn or fail and never ranks or picks.
- **Bounded context**: a `Brief` with only what its checks need. The real
  handoff from the run above:

  ```json
  {"card_id": "citi-aadvantage-platinum-select-world-elite",
   "claimed_year1": 1120.0, "claimed_steady": -99.0,
   "max_annual_fee": 400.0, "monthly_spend": 1000.0, "credit_score_band": null,
   "wallet": [{"card_id": "amex-gold", "opened_on": "2025-01-10", …}, …]}
  ```

  It does not get spending by category, point values, the other
  candidates, or the conversation. A test pins the brief's exact keys. Its
  tools are read-only: public card data, issuer rules, the freshness guard
  and the official-link check.
- **Expected output**: a `Report` with a verdict and one line of evidence
  per check. Checks: available, fee_cap, issuer_rules, min_spend,
  offer_dates, terms_fresh, issuer_page, after_year_one, credit_fit,
  official_link, data_age. From the run above:

  ```
  ✅ Open to new applicants.
  ✅ $99 annual fee (waived the first year) is within your $400 limit.
  ✅ No known issuer rule blocks you.
  ✅ The $3,500 minimum spend in 120 days fits your usual ~$3,943.
  ✅ Fee and rewards verified on the issuer's page on Sep 27.
  ⚠️ After year 1 it costs about $99/yr more than it adds: plan to downgrade or cancel before the second annual fee.
  ✅ Official application page on www.citi.com.
  ✅ Offer data is 1 day old.
  ```

- **How the Advisor uses it**: fail means reject the card and hand over the
  next one. After five failures on minimum spend, it revises the plan once.
  Pass or warn means accept, and every warning is shown to the person as a
  caveat. If nothing passes, it asks the person. `verify "X"` runs the same
  Verifier on one card when the person asks "should I get X?".

The chat model can also delegate. SKILL.md gives a sub-agent brief for
OpenClaw's `sessions_spawn` when the person asks to vet several cards at
once: one card per sub-agent, `verify` only, no other actions. It also says
how to check the result (a verdict plus the ✅/⚠️/❌ lines, else rerun
`verify` yourself).

A second delegation runs on GitHub Actions. The card-terms pipeline hands
one issuer page at a time to an LLM **Extractor** (role: read one page and
return earn rates, credits and fees with a verbatim quote for each). Its
output is checked deterministically by `card_agent/terms/validate.py`: every
quote must be on the page, and every number must be in its quote. Only then
is it proposed in a PR. The last full run verified 51 of 55 tracked cards
and rejected 4.

## 5. Failure detection and recovery

**The intentional failure: the card-data server is unreachable** during
"refresh my card data" (a real risk: container network blips, GitHub
outages, a deleted branch).

1. **Detect**: each download try records its error (`ConnectError`,
   `HTTP 503`, …).
2. **Retry**: a transient error (network, 429, 5xx) is retried once after a
   1-second pause.
3. **Switch tools**: then it tries the other endpoint (raw.githubusercontent.com
   ↔ the GitHub contents API). A 401/403 switches at once, without a retry.
4. **Fall back**: if every try fails, the saved snapshot is kept. It is only
   ever replaced by a download that validated, written atomically, so a
   half-download can't corrupt it. The person is told plainly: "⚠️ I
   couldn't reach the card data server (no connection, 4 tries on two
   routes). I'm still using the saved copy from 1 day ago, so everything
   keeps working."
5. **Escalate**: only when there is no saved copy does it stop and ask
   (`next.action = ask_user`).

`advise` does the same inside its loop when the data is over 7 days old. It
logs "Refresh failed after 4 tries (no connection); a saved copy from 10
days ago is still here" and carries on with a caveat. The digest does the
same.

The failure is injected three ways: in tests with `httpx.MockTransport`
(`tests/test_recovery.py`: retry, switch, 404 vs 403, fallback, no saved
copy, and the CLI and `advise` paths); in the evaluation with a dead proxy;
and live on Maritime with the demo command below.

Other failures it detects and recovers from:

| Failure | Detection | Recovery |
|---|---|---|
| Typo in a card name ("saphire prefered") | no exact match | Read commands take the one clear match and say so ("🔤 I read 'saphire prefered' as Chase Sapphire Preferred"); write commands ask "Did you mean …?" |
| The top cards' minimum spend is out of reach | the Verifier fails them | Reject, try the next; after five, revise the plan to reachable cards |
| No spending profile | memory read comes back empty | Ask the five onboarding questions |
| No card data yet | `SnapshotMissing` | `next = run sync, then retry` |
| The chat model sends a malformed command | argparse error | `next = fix_command`; the model fixes it quietly, once |
| Digest email fails | send raises | The chat digest still goes out, with a line saying the email didn't |

## 6. Evaluation: baseline vs improved

`scripts/eval_agent.py` plays five scripted conversations against two
configurations on the same pinned card data (the `data` branch at
`3ae2582`, generated 2026-09-27), each with a fresh state DB:

- **baseline**: the code and SKILL.md at `a568dd7` (main before this change);
- **improved**: this branch.

Each conversation follows the policy its own SKILL.md prescribes; for
example, "what card should I get?" runs `rank` in the baseline and `advise`
in the improved version. No LLM is called, and the outage is simulated
with a dead proxy, so the run is reproducible and free.

| Scenario | Config | Success | Tool calls | Human interventions | Latency (ms) | Tokens read | Quality issues |
|---|---|---|---|---|---|---|---|
| S1 New user asks for a card | baseline | ❌ | 1 | 1 | 725 | 24 | jargon: onboard |
| S1 New user asks for a card | improved | ✅ | 1 | 1 | 581 | 209 | none |
| S2 Recommendation for a $1,000/mo spender | baseline | ❌ | 1 | 0 | 694 | 1,766 | markup: html tag |
| S2 Recommendation for a $1,000/mo spender | improved | ✅ | 1 | 0 | 764 | 1,227 | none |
| S3 Typo in a card name | baseline | ❌ | 2 | 1 | 1,244 | 1,970 | none |
| S3 Typo in a card name | improved | ✅ | 1 | 0 | 739 | 1,853 | none |
| S4 Card data server down | baseline | ❌ | 1 | 1 | 781 | 21 | jargon: Errno |
| S4 Card data server down | improved | ✅ | 1 | 0 | 2,733 | 259 | none |
| S5 Remembers a rejected card | baseline | ❌ | 2 | 1 | 1,358 | 3,136 | markup: html tag |
| S5 Remembers a rejected card | improved | ✅ | 3 | 0 | 2,388 | 1,784 | none |
| C  Typical spender asks for a card (control) | baseline | ✅ | 1 | 0 | 742 | 1,568 | markup: html tag |
| C  Typical spender asks for a card (control) | improved | ✅ | 1 | 0 | 783 | 872 | none |

Summary over S1-S5 (the control row is not scored):

| Config | Success rate | Tool calls | Human interventions | Median latency per call (ms) | Tokens read | Quality issues | Tool cost |
|---|---|---|---|---|---|---|---|
| baseline | 0/5 | 7 | 4 | 694 | 6,917 | 4 | $0.00 |
| improved | 5/5 | 7 | 1 | 764 | 5,332 | 0 | $0.00 |

What each scenario checks:

- **S1**: success means the agent asks for the missing details in plain
  words. The baseline answers "No spending profile yet. Run onboard first."
  (a command name the person can't act on); the improved agent asks five
  plain questions. Both need the person once; that is the correct ask.
- **S2**: success means the first recommended card survives the Verifier,
  run separately as a judge on the same profile. The baseline's top pick
  needs $4,000 in 120 days from someone who spends about $3,943 in that
  time. The improved loop rejects five such cards, revises the plan and
  picks one whose minimum fits.
- **S3**: success means the right card is explained without asking the
  person to retype it.
- **S4**: success means the agent recovers and says the saved data still
  works. The baseline shows "[Errno 111] Connection refused".
- **S5**: success means a card the person rejected is not recommended in
  the next session.
- **Control**: the happy path the baseline already handled. Same pick,
  same single call, no regression, and fewer tokens.

Metric definitions:

- **Tool calls**: CLI commands run for the request.
- **Human interventions**: times the agent must go back to the person
  before the goal is met.
- **Latency**: wall time of the tool calls, mostly Python start-up.
- **Tokens read**: tool stdout characters ÷ 4, what the chat model must
  read.
- **Quality issues**: markup, command names or exception names in text
  shown to the person. The baseline's "html tag" is its tip
  `Ask "explain <card>"`: web chat treats `<card>` as an HTML tag and drops
  it.
- **Cost**: the tools make no paid API calls, so tool cost is $0.00. The
  chat model's cost scales with tokens read, which fell 23%.

Trade-offs, stated plainly:

- Recovery costs time. S4 takes about 2 s longer: two 1-second back-offs
  before falling back.
- S5 takes one more tool call, because remembering requires a write.
- The scenarios were chosen to exercise the new behaviors, which is why the
  control row is included.
- The policies are scripted from each SKILL.md, not driven by a live chat
  model. Use the live script below to check the same scenarios on Maritime.

Reproduce:

```bash
git show 3ae2582:data/latest.json > /tmp/latest.json
python scripts/eval_agent.py --snapshot /tmp/latest.json --json /tmp/eval.json
```

## Live demo on Maritime (web chat or WhatsApp)

Send these after re-running `deploy/maritime_setup.sh`. The text in
brackets is what to look for.

1. "What card should I get?" [🏆 pick with 🕵️ Verifier line, caveats, the
   issuer's own link, a pre-approval link and a bar chart; no code blocks]
2. "How did you decide?" [🧭 the loop: 🎯 goal, 🤔 decide, 🛠️ act, 👀 observe,
   ⚖️ evaluate, 🕵️ handoff, 📋 result, 🛑 stop]
3. "I'm never getting the <that card>, stop suggesting it." [🙈 saved], then
   start a new chat and ask 1 again [a different pick, "🔄 Changed since…",
   "🙈 Skipping 1 card…"]
4. "What do you remember about me?" [🧠 profile, spending, wallet, hidden
   card with your reason, recent picks]
5. "Explain the saphire prefered" [🔤 "I read … as Chase Sapphire Preferred"]
6. "Should I get the Sapphire Reserve?" [🕵️ Verifier check; ❌ if its fee
   is over your limit]
7. "Which card should I use for groceries?" [🛒 best card from your wallet]
8. "How can I grow my credit score?" [🌱 your 5/24 count, account age,
   FICO factor chart, habits, official links]
9. Failure demo, in Maritime's Console:

   ```bash
   HTTPS_PROXY=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 \
     /data/.openclaw/workspace/skills/credit_card_deal_agent/bin/card-agent sync
   ```

   [`"status": "saved_copy"` and "⚠️ I couldn't reach the card data
   server (no connection, 4 tries on two routes). I'm still using the saved
   copy…"]. Then ask in chat "What did you do?": the trace lists that sync
   and its outcome.
