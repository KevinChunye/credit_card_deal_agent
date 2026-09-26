# Test fixtures

All tests run offline against these files. HTTP and AgentMail are mocked.

- `bonuses_api_data.json`: 26 cards copied from
  [andenacitelli/credit-card-bonuses-api](https://github.com/andenacitelli/credit-card-bonuses-api)
  `exports/data.json` (MIT License, Copyright (c) 2023 Anden Acitelli), trimmed on 2026-09-26.
- `doc_feed_page1.xml`, `doc_feed_page2.xml`: WordPress RSS 2.0 in Doctor of Credit's
  format. Titles on page 1 are real titles the probe saw on 2026-09-26; the rest,
  and all descriptions and links, are written for the tests (including one
  prompt-injection title that must be treated as plain data).
- `issuer_pages/*.html`: trimmed stand-ins modeled on text the issuer probe
  found (static offer text, an Amex-style inline state blob that bundles other
  cards' promos, and a bot-protection page). Not copies of issuer pages.
- `rewards_db/`: one synthetic card in the fuermosi777/rewards schema with
  made-up values. That repository has no license, so nothing is copied from it.
- `emails/*.json`: synthetic AgentMail messages (forwarded offer, direct issuer
  offer, phishing lookalike, non-allowlisted sender, Gmail forwarding confirmation).
