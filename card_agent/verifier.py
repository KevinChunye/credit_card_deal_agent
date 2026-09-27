"""The Verifier: a subagent that checks one card before it is recommended.

Role: an independent second look. The Advisor (the main loop, advisor.py)
ranks cards and proposes one; the Verifier checks that one card and answers
pass, warn or fail. It never ranks, never picks, and never talks to you.

Bounded context: it gets a Brief with only what its checks need: the card,
the Advisor's claimed values, your fee cap, your total monthly spend, your
self-reported score band, and your wallet's open/close dates (for issuer
rules). It does not see your spending by category, your point valuations, the
other candidates or the conversation, so neither the ranking nor a pushy
message can argue it into a pass. Its tools are read-only: the public card
data, the issuer rules, the terms-freshness guard and the official-link check.

Expected output: a Report with a verdict (fail if any check fails, else warn
if any warns, else pass) and one line of evidence per check. The same
Verifier runs inside `advise` and on its own as `verify "<card>"`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from card_agent.eligibility import EligibilityChecker
from card_agent.freshness import terms_warning
from card_agent.links import apply_link, host
from card_agent.models import Card, EligibilityRule, WalletCard
from card_agent.present import plural
from card_agent.scoring import DAYS_PER_MONTH, CardEvaluation, ScoringContext
from card_agent.snapshot import DataView

ROLE = (
    "Verifier: independently check one proposed card (availability, fee cap, issuer "
    "rules, minimum spend, offer dates, terms freshness, issuer-page cross-check, value "
    "after year 1, credit fit, official link, data age) and return pass, warn or fail "
    "with evidence. Sees only the brief; cannot rank, pick or act."
)
ENDS_SOON_DAYS = 14
DATA_STALE_DAYS = 14
PREMIUM_FEE = 250.0
ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
FIELD_LABEL = {"annual_fee": "annual fee", "bonus_amount": "sign-up bonus"}


def _money(value: float) -> str:
    return f"${value:,.0f}"


@dataclass(frozen=True)
class Brief:
    """Everything the Verifier is told. Nothing else crosses the handoff."""

    card_id: str
    claimed_year1: float
    claimed_steady: float
    max_annual_fee: float
    monthly_spend: float
    credit_score_band: str | None
    wallet: tuple[WalletCard, ...]

    @classmethod
    def for_candidate(
        cls, ev: CardEvaluation, ctx: ScoringContext, max_annual_fee: float | None = None
    ) -> Brief:
        return cls(
            card_id=ev.card_id,
            claimed_year1=round(ev.marginal_ev_year1, 2),
            claimed_steady=round(ev.marginal_ev_steady, 2),
            max_annual_fee=(
                ctx.profile.max_annual_fee if max_annual_fee is None else max_annual_fee
            ),
            monthly_spend=round(sum(ctx.spend.values()), 2),
            credit_score_band=ctx.profile.credit_score_band,
            wallet=tuple(ctx.wallet),
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["wallet"] = [w.model_dump(mode="json", exclude_none=True) for w in self.wallet]
        return data


@dataclass
class Check:
    name: str
    status: str  # pass | warn | fail
    detail: str

    @property
    def line(self) -> str:
        return f"{ICON[self.status]} {self.detail}"


@dataclass
class Report:
    card_id: str
    card_name: str
    verdict: str
    checks: list[Check] = field(default_factory=list)
    apply_url: str | None = None

    def with_status(self, status: str) -> list[Check]:
        return [check for check in self.checks if check.status == status]

    @property
    def summary(self) -> str:
        parts = [f"✅ {len(self.with_status('pass'))} passed"]
        if self.with_status("warn"):
            parts.append(f"⚠️ {plural(len(self.with_status('warn')), 'caveat')}")
        if self.with_status("fail"):
            parts.append(f"❌ {len(self.with_status('fail'))} failed")
        return " · ".join(parts)

    @property
    def headline(self) -> str:
        return {
            "pass": "✅ Looks good",
            "warn": "⚠️ OK, with caveats",
            "fail": "❌ Not recommended right now",
        }[self.verdict]

    def to_dict(self) -> dict:
        return {
            "card_id": self.card_id,
            "card_name": self.card_name,
            "verdict": self.verdict,
            "summary": self.summary,
            "checks": [asdict(check) for check in self.checks],
            "apply_url": self.apply_url,
        }


class Verifier:
    role = ROLE

    def __init__(
        self,
        data: DataView,
        rules: list[EligibilityRule],
        today: date,
        data_age_days: float | None,
    ):
        self.data = data
        self.rules = rules
        self.today = today
        self.data_age_days = data_age_days

    def run(self, brief: Brief) -> Report:
        card = self.data.cards[brief.card_id]
        checks = [
            self._available(card, brief),
            self._fee(card, brief),
            self._eligibility(card, brief),
            *self._offer(card, brief),
            self._terms(card),
            *self._cross_check(card),
            self._after_year_one(brief),
            *self._credit_fit(card, brief),
            self._link(card),
            *self._data_age(),
        ]
        statuses = {check.status for check in checks}
        verdict = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "pass")
        return Report(card.id, card.display_name, verdict, checks, apply_link(card))

    # ------------------------------------------------------------------ checks
    def _available(self, card: Card, brief: Brief) -> Check:
        if card.discontinued:
            return Check("available", "fail", "No longer accepting applications.")
        if any(w.card_id == card.id and w.is_open for w in brief.wallet):
            return Check("available", "fail", "You already have this card.")
        return Check("available", "pass", "Open to new applicants.")

    def _fee(self, card: Card, brief: Brief) -> Check:
        waived = " (waived the first year)" if card.first_year_fee_waived else ""
        if card.annual_fee <= brief.max_annual_fee:
            return Check(
                "fee_cap",
                "pass",
                f"{_money(card.annual_fee)} annual fee{waived} is within your "
                f"{_money(brief.max_annual_fee)} limit.",
            )
        return Check(
            "fee_cap",
            "fail",
            f"{_money(card.annual_fee)} annual fee is over your {_money(brief.max_annual_fee)} limit.",
        )

    def _eligibility(self, card: Card, brief: Brief) -> Check:
        checker = EligibilityChecker(self.rules, list(brief.wallet), self.data.cards, self.today)
        outcome = checker.check(card)
        reasons = "; ".join(outcome.reasons)
        if outcome.status == "ineligible":
            return Check("issuer_rules", "fail", f"An issuer rule blocks you: {reasons}.")
        if outcome.status == "unknown":
            return Check("issuer_rules", "warn", f"Can't confirm an issuer rule: {reasons}.")
        return Check("issuer_rules", "pass", "No known issuer rule blocks you.")

    def _offer(self, card: Card, brief: Brief) -> list[Check]:
        offer = self.data.offers.get(card.id)
        if offer is None:
            return [
                Check("min_spend", "pass", "No sign-up bonus to chase; the value is the rewards.")
            ]
        checks = []
        if offer.expires_at:
            days = (offer.expires_at - self.today).days
            if days < 0:
                checks.append(
                    Check("offer_dates", "fail", f"The offer ended {offer.expires_at:%b %d}.")
                )
            elif days <= ENDS_SOON_DAYS:
                checks.append(
                    Check(
                        "offer_dates",
                        "warn",
                        f"The offer ends {offer.expires_at:%b %d}; it may change after that.",
                    )
                )
        if offer.min_spend:
            window = offer.spend_window_days or 90
            usual = brief.monthly_spend * window / DAYS_PER_MONTH
            if usual >= offer.min_spend:
                checks.append(
                    Check(
                        "min_spend",
                        "pass",
                        f"The {_money(offer.min_spend)} minimum spend in {window} days fits your "
                        f"usual ~{_money(usual)}.",
                    )
                )
            else:
                checks.append(
                    Check(
                        "min_spend",
                        "fail",
                        f"Needs {_money(offer.min_spend)} in {window} days, but your usual spending "
                        f"is ~{_money(usual)}. Don't manufacture spend for a bonus.",
                    )
                )
        return checks

    def _terms(self, card: Card) -> Check:
        warning = terms_warning(card, self.today)
        if warning is None:
            return Check(
                "terms_fresh",
                "pass",
                f"Fee and rewards verified on the issuer's page on {card.last_verified:%b %d}.",
            )
        return Check(
            "terms_fresh",
            "warn",
            f"{warning[0].upper()}{warning[1:]}; confirm on the issuer's page.",
        )

    def _cross_check(self, card: Card) -> list[Check]:
        rows = [row for row in self.data.snapshot.cross_checks if row.get("card_id") == card.id]
        if not rows:
            return []
        mismatches = [row for row in rows if not row.get("match")]
        if not mismatches:
            fields = sorted({FIELD_LABEL.get(row["field"], row["field"]) for row in rows})
            verb = "matches" if len(fields) == 1 else "match"
            return [
                Check(
                    "issuer_page",
                    "pass",
                    f"The {' and '.join(fields)} {verb} the issuer's own page.",
                )
            ]
        detail = "; ".join(
            f"{FIELD_LABEL.get(row['field'], row['field'])}: offer feed "
            f"{row.get('api_value') or 0:,.0f}, issuer page {row.get('page_value') or 0:,.0f}"
            for row in mismatches
        )
        return [
            Check("issuer_page", "warn", f"Sources disagree ({detail}); trust the issuer's page.")
        ]

    def _after_year_one(self, brief: Brief) -> Check:
        if brief.claimed_steady >= 0:
            return Check(
                "after_year_one",
                "pass",
                f"Still adds about {_money(brief.claimed_steady)}/yr after the first year.",
            )
        return Check(
            "after_year_one",
            "warn",
            f"After year 1 it costs about {_money(-brief.claimed_steady)}/yr more than it adds: "
            "plan to downgrade or cancel before the second annual fee.",
        )

    def _credit_fit(self, card: Card, brief: Brief) -> list[Check]:
        band = brief.credit_score_band
        if band is None:
            return []
        if band == "building" and card.annual_fee > 0:
            return [
                Check(
                    "credit_fit",
                    "warn",
                    "Cards with an annual fee usually need an established score; check "
                    "pre-approval first.",
                )
            ]
        if band == "fair" and card.annual_fee >= PREMIUM_FEE:
            return [
                Check(
                    "credit_fit",
                    "warn",
                    "Premium cards usually need good credit (670+); check pre-approval first.",
                )
            ]
        return [Check("credit_fit", "pass", "Your score range fits this card.")]

    def _link(self, card: Card) -> Check:
        url = apply_link(card)
        if url:
            return Check("official_link", "pass", f"Official application page on {host(url)}.")
        return Check(
            "official_link",
            "warn",
            "No issuer link I can vouch for; apply only on the issuer's own website.",
        )

    def _data_age(self) -> list[Check]:
        if self.data_age_days is None:
            return []
        days = self.data_age_days
        age = plural(round(days), "day")
        if days <= DATA_STALE_DAYS:
            return [Check("data_age", "pass", f"Offer data is {age} old.")]
        return [
            Check(
                "data_age",
                "warn",
                f"Offer data is {age} old and bonuses change often; re-check the issuer's page.",
            )
        ]
