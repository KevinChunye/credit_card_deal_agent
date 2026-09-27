"""One run of the card-terms pipeline.

For each tracked card in config/card_sources.yaml:

    no url                          -> source_status "manual"; hand values kept
    fetch fails                     -> "fetch_failed"; values kept
    page hash unchanged             -> last_verified = today; no LLM call
    hash changed, queued by the     -> one LLM call, then deterministic validation:
    RSS trigger, or forced             "ok" (terms stored) or "validation_failed"

Pages are fetched one at a time (robots.txt, per-host throttle); only the LLM
calls run in parallel. Validated terms are diffed against
config/card_details.yaml, and the workflow turns the diff into a reviewed PR.

Modes:
    full       every tracked card (monthly)
    queue      only cards the RSS trigger queued (weekly; usually none)
    smoke      the given cards, forced, nothing saved (PR check)
    bootstrap  every card, forced, nothing saved, validated without falling back
               to hand values (for comparing extraction against the hand YAML)
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from card_agent.collector.http import HostThrottle, RobotsCache
from card_agent.terms.details import DiffRow, apply_terms, diff_terms, terms_from_entry
from card_agent.terms.extract import extract_terms
from card_agent.terms.llm import (
    DEFAULT_MAX_RUN_COST_USD,
    LLMError,
    LLMResult,
    Provider,
    Usage,
    estimate_cost,
)
from card_agent.terms.page import FetchedPage, fetch_page
from card_agent.terms.schema import CardTerms, TermsExtraction
from card_agent.terms.sources import CardSource
from card_agent.terms.state import CardState, CardTermsStore, PageHashes, TermsQueue
from card_agent.terms.validate import ValidationResult, merge_terms, validate_extraction

MODES = ("full", "queue", "smoke", "bootstrap")
LLM_WORKERS = 4

# Outcome actions, in the order reports list them.
ACTIONS = {
    "extracted": "extracted and validated",
    "rejected": "extraction rejected",
    "llm_error": "LLM call failed (retried next run)",
    "over_budget": "not sent: run cost cap reached (retried next run)",
    "no_llm": "needs extraction, no LLM configured",
    "unchanged": "page unchanged, re-verified (no LLM call)",
    "unchanged_failed": "page unchanged since a rejected extraction (no LLM call)",
    "fetch_failed": "fetch failed",
    "manual": "no usable page (manual)",
}


@dataclass
class RunOptions:
    mode: str = "full"
    cards: list[str] | None = None  # restrict the run to these card ids
    force: bool = False  # extract even if the page hash is unchanged
    # Diff every stored extraction, not just this run's (the auto PR is still open,
    # so its unmerged proposals must stay in it).
    include_pending: bool = False


@dataclass
class NotSent:
    """An LLM call the spend cap stopped before it was made."""

    reason: str


@dataclass
class PipelineState:
    hashes: PageHashes = field(default_factory=PageHashes)
    terms: CardTermsStore = field(default_factory=CardTermsStore)
    queue: TermsQueue = field(default_factory=TermsQueue)


@dataclass
class CardOutcome:
    source: CardSource
    action: str
    status: str
    why: str = ""  # why the LLM was (or would have been) called
    note: str = ""
    text_chars: int = 0
    sha256: str | None = None
    usage: Usage = field(default_factory=Usage)
    extraction: TermsExtraction | None = None
    validation: ValidationResult | None = None

    @property
    def card_id(self) -> str:
        return self.source.card_id


@dataclass
class RunReport:
    mode: str
    today: date
    provider: str | None
    model: str | None
    provider_note: str | None
    outcomes: list[CardOutcome]
    usage: Usage
    diffs: list[DiffRow] = field(default_factory=list)
    queued: dict[str, str] = field(default_factory=dict)  # card id -> post title, left queued
    llm_problem: str | None = None  # a provider error that stopped extraction this run
    max_cost: float | None = None  # MAX_RUN_COST_USD for this run
    budget_note: str | None = None  # why the spend cap stopped further LLM calls

    @property
    def cost(self) -> float | None:
        return estimate_cost(self.usage, self.model) if self.model else None

    @property
    def changed_cards(self) -> list[str]:
        unique: list[str] = []
        for row in self.diffs:
            if row.card_id not in unique:
                unique.append(row.card_id)
        return unique


class Pipeline:
    def __init__(
        self,
        *,
        client: httpx.Client,
        sources: dict[str, CardSource],
        details: dict[str, Any],
        state: PipelineState,
        today: date,
        provider: Provider | None = None,
        provider_note: str | None = None,
        throttle: HostThrottle | None = None,
        workers: int = LLM_WORKERS,
        max_cost: float = DEFAULT_MAX_RUN_COST_USD,
    ):
        self.client = client
        self.robots = RobotsCache(client)
        self.throttle = throttle or HostThrottle()
        self.sources = sources
        self.details = details
        self.state = state
        self.today = today
        self.provider = provider
        self.provider_note = provider_note
        self.workers = workers
        self.max_cost = max_cost
        self.llm_problem: str | None = None
        self.budget_note: str | None = None

    # ----------------------------------------------------------------- run

    def run(self, options: RunOptions) -> RunReport:
        if options.mode not in MODES:
            raise ValueError(f"unknown mode {options.mode!r}; expected one of {MODES}")
        outcomes: list[CardOutcome] = []
        pending: list[tuple[CardOutcome, FetchedPage]] = []
        for source, why in self.select(options):
            outcome, page = self.check_page(source, why)
            outcomes.append(outcome)
            if page is not None:
                pending.append((outcome, page))

        if pending and self.provider is None:
            for outcome, _page in pending:
                outcome.action, outcome.note = "no_llm", self.provider_note or "no LLM provider"
                self.keep_status(outcome)
        elif pending:
            results = self.extract_all(pending)
            for (outcome, page), result in zip(pending, results, strict=True):
                self.finish(outcome, page, result, merge_previous=options.mode != "bootstrap")

        if options.mode in ("full", "queue"):
            self.update_queue(outcomes)
        usage = Usage()
        for outcome in outcomes:
            usage.add(outcome.usage)
        extracted = {o.card_id for o in outcomes if o.action == "extracted"}
        return RunReport(
            mode=options.mode,
            today=self.today,
            provider=getattr(self.provider, "name", None),
            model=getattr(self.provider, "model", None),
            provider_note=self.provider_note,
            outcomes=outcomes,
            usage=usage,
            diffs=self.proposals(extracted, options.include_pending),
            queued={k: v.reason for k, v in self.state.queue.queued.items()},
            llm_problem=self.llm_problem,
            max_cost=self.max_cost if self.provider is not None else None,
            budget_note=self.budget_note,
        )

    def select(self, options: RunOptions) -> list[tuple[CardSource, str | None]]:
        """The cards this run looks at, each with the reason it is forced through
        extraction (None: only if its page changed)."""
        wanted = set(options.cards or [])
        unknown = wanted - set(self.sources)
        if unknown:
            raise ValueError(f"unknown card ids: {', '.join(sorted(unknown))}")
        queued = self.state.queue.queued
        picked: list[tuple[CardSource, str | None]] = []
        for card_id, source in self.sources.items():
            if wanted and card_id not in wanted:
                continue
            if options.mode == "queue" and card_id not in queued:
                continue
            if options.mode in ("smoke", "bootstrap"):
                why = options.mode
            elif options.force:
                why = "forced"
            elif card_id in queued:
                why = f"queued by RSS: {queued[card_id].reason}"
            else:
                why = None
            picked.append((source, why))
        return picked

    # ------------------------------------------------------------- per card

    def card_state(self, card_id: str) -> CardState:
        state = self.state.hashes.cards.get(card_id)
        if state is None:
            state = CardState()
            self.state.hashes.cards[card_id] = state
        return state

    def check_page(
        self, source: CardSource, why: str | None
    ) -> tuple[CardOutcome, FetchedPage | None]:
        """Fetch and hash. Returns the page only if it needs an LLM call."""
        state = self.card_state(source.card_id)
        if source.is_manual:
            state.url, state.source_status = None, "manual"
            return CardOutcome(source, "manual", "manual", note=source.manual_reason or ""), None

        page = fetch_page(self.client, self.robots, self.throttle, source.url)
        state.url = source.url
        if not page.ok:
            state.source_status = "fetch_failed"
            return CardOutcome(source, "fetch_failed", "fetch_failed", note=page.error or ""), None
        state.last_fetched = self.today
        outcome = CardOutcome(
            source, "pending", state.source_status, text_chars=len(page.text), sha256=page.sha256
        )
        if why is None and state.sha256 == page.sha256:
            if state.extraction_status == "ok":
                state.last_verified, state.source_status = self.today, "ok"
                outcome.action, outcome.status = "unchanged", "ok"
                return outcome, None
            if state.extraction_status == "validation_failed":
                state.source_status = "validation_failed"
                outcome.action, outcome.status = "unchanged_failed", "validation_failed"
                outcome.note = "not retried until the page changes (or --force)"
                return outcome, None
        outcome.why = why or ("new page" if state.sha256 is None else "page changed")
        return outcome, page

    def extract_all(
        self, pending: list[tuple[CardOutcome, FetchedPage]]
    ) -> list[LLMResult | LLMError | NotSent]:
        """LLM calls, a few at a time. After a fatal error (bad key, unknown model)
        the remaining cards aren't sent. Once the estimated cost of this run's
        calls reaches max_cost, no new call starts (calls already running finish,
        so a run can end at most a few calls over the cap)."""
        stopped = threading.Event()
        lock = threading.Lock()
        spent = Usage()

        def budget_problem() -> str | None:
            cost = estimate_cost(spent, self.provider.model)
            if cost is None:
                return (
                    f"MAX_RUN_COST_USD can't be enforced: no price is known for "
                    f"{self.provider.model!r}; set LLM_PRICE_INPUT_PER_MTOK and "
                    "LLM_PRICE_OUTPUT_PER_MTOK"
                )
            if cost >= self.max_cost:
                return f"estimated cost ${cost:.4f} reached MAX_RUN_COST_USD ${self.max_cost:.2f}"
            return None

        def call(item: tuple[CardOutcome, FetchedPage]) -> LLMResult | LLMError | NotSent:
            outcome, page = item
            if stopped.is_set():
                return LLMError(f"not sent: {self.llm_problem}")
            with lock:
                problem = self.budget_note or budget_problem()
                if problem:
                    self.budget_note = problem
                    return NotSent(problem)
            try:
                result = extract_terms(self.provider, outcome.source, page.text)
            except LLMError as exc:
                with lock:
                    spent.add(exc.usage)
                if exc.fatal:
                    self.llm_problem = str(exc)
                    stopped.set()
                return exc
            with lock:
                spent.add(result.usage)
            return result

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(call, pending))

    def previous_terms(self, card_id: str) -> CardTerms | None:
        entry = (self.details.get("cards") or {}).get(card_id)
        return terms_from_entry(entry) if entry is not None else None

    def finish(
        self,
        outcome: CardOutcome,
        page: FetchedPage,
        result: LLMResult | LLMError | NotSent,
        merge_previous: bool = True,
    ) -> None:
        """Validate one extraction and record it. The page hash is stored only once
        an extraction has been judged, so a failed LLM call is retried next run."""
        state = self.card_state(outcome.card_id)
        if isinstance(result, NotSent):
            outcome.action, outcome.note = "over_budget", f"not sent: {result.reason}"
            self.keep_status(outcome)
            return
        outcome.usage = result.usage
        if isinstance(result, LLMError):
            outcome.action, outcome.note = "llm_error", str(result)
            self.keep_status(outcome)
            return
        outcome.extraction = result.parsed
        previous = self.previous_terms(outcome.card_id) if merge_previous else None
        validation = validate_extraction(result.parsed, page.text, outcome.source, previous)
        outcome.validation = validation

        state.sha256 = page.sha256
        state.last_extracted = self.today
        state.model = result.model
        state.issues = [issue.as_dict() for issue in validation.issues]
        if validation.rejected:
            state.extraction_status = state.source_status = "validation_failed"
            state.issues.insert(0, {"field": "card", "reason": validation.reason or ""})
            outcome.action, outcome.status = "rejected", "validation_failed"
            outcome.note = validation.reason or ""
            return
        state.extraction_status = state.source_status = "ok"
        state.last_verified = self.today
        outcome.action, outcome.status = "extracted", "ok"
        if validation.issues:
            outcome.note = f"{len(validation.issues)} field(s) failed validation; kept on file"
        self.state.terms.cards[outcome.card_id] = validation.validated

    def keep_status(self, outcome: CardOutcome) -> None:
        """Fetched but not (re-)extracted: the status of the last judged extraction
        stands ("manual" if there never was one), and last_verified doesn't move."""
        state = self.card_state(outcome.card_id)
        state.source_status = outcome.status = state.extraction_status or "manual"

    def update_queue(self, outcomes: list[CardOutcome]) -> None:
        """A queued card leaves the queue once an extraction has been judged; after a
        fetch or LLM failure it stays (queue entries expire after a few weeks)."""
        done = {o.card_id for o in outcomes if o.action in ("extracted", "rejected", "manual")}
        self.state.queue.queued = {
            card_id: entry
            for card_id, entry in self.state.queue.queued.items()
            if card_id not in done
        }

    # ------------------------------------------------------------ proposals

    def proposed_terms(self, card_id: str) -> tuple[CardTerms, CardTerms] | None:
        """(terms on file, terms with the stored extraction applied), if both exist."""
        stored = self.state.terms.cards.get(card_id)
        on_file = self.previous_terms(card_id)
        if stored is None or on_file is None:
            return None
        return on_file, merge_terms(stored, on_file)[0]

    def proposals(self, extracted_now: set[str], include_pending: bool) -> list[DiffRow]:
        """Validated terms that differ from config/card_details.yaml.

        Normally only this run's extractions are proposed, so a closed (declined)
        PR isn't reopened until the page changes again. While the auto PR is open
        (`include_pending`), every stored extraction is diffed so the PR keeps its
        unmerged proposals.
        """
        candidates = set(extracted_now)
        if include_pending:
            candidates |= set(self.state.terms.cards)
        rows: list[DiffRow] = []
        for card_id, source in self.sources.items():
            pair = self.proposed_terms(card_id) if card_id in candidates else None
            if pair is None or source.is_manual:
                continue
            rows += diff_terms(card_id, pair[0], pair[1], source.url or "")
        return rows

    def apply(self, report: RunReport) -> dict[str, Any]:
        """card_details.yaml content with every proposed card's terms applied, and
        as_of set to this run's month."""
        cards = dict(self.details.get("cards") or {})
        for card_id in report.changed_cards:
            state = self.card_state(card_id)
            _on_file, proposed = self.proposed_terms(card_id)
            cards[card_id] = apply_terms(
                cards[card_id],
                proposed,
                self.sources[card_id].url,
                state.last_extracted or self.today,
                state.model,
            )
        return {**self.details, "as_of": self.today.strftime("%Y-%m"), "cards": cards}
