"""Card-terms pipeline: keep config/card_details.yaml in sync with issuer pages.

fetch page -> hash -> (only if changed) LLM extraction with verbatim evidence ->
deterministic validation -> diff vs the YAML -> reviewed PR. See docs/ARCHITECTURE.md.
Scoring never calls this code; it only reads the reviewed YAML.
"""
