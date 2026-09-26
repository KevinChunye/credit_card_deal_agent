"""Monthly digest: a short WhatsApp version and a full email version.

Sections: top marginal-EV opportunities you're eligible for, new/elevated
bonuses from the latest changes file, personal offers from your inbox,
annual fees due in the next 60 days (keep/downgrade), credits you're likely
leaving unused, and relevant Doctor of Credit news.

Email and scraped text is untrusted: the short version never includes raw
email subjects, and every scraped string goes through sanitize_untrusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from card_agent.guardrails import sanitize_untrusted
from card_agent.models import BenefitKind, ChangeSet, PersonalOffer
from card_agent.scoring import (
    ScoringContext,
    benefit_face_usd,
    cards_opened_last_12_months,
    evaluate,
    rank,
)

WHATSAPP_LIMIT = 1500
FEE_WINDOW_DAYS = 60
NEWS_WINDOW_DAYS = 35
# Benefits that are credits you can fail to use (lounge access and status aren't).
CREDIT_KINDS = set(BenefitKind) - {BenefitKind.lounge, BenefitKind.elite_status}


@dataclass
class Item:
    short: str
    full: str
    data: dict = field(default_factory=dict)


@dataclass
class Section:
    key: str
    title: str
    items: list[Item] = field(default_factory=list)
    note: str | None = None


@dataclass
class Digest:
    period: str
    title: str
    short: str
    full: str
    sections: list[Section]

    def to_dict(self) -> dict:
        return {
            "period": self.period,
            "title": self.title,
            "sections": {
                s.key: {"title": s.title, "note": s.note, "items": [i.data for i in s.items]}
                for s in self.sections
            },
        }


def money(value: float, signed: bool = False) -> str:
    sign = ("+" if value >= 0 else "−") if signed else ("−" if value < 0 else "")
    return f"{sign}${abs(value):,.0f}"


def next_occurrence(anchor: date, today: date) -> date:
    """The next date on or after `today` with anchor's month and day."""
    for year in (today.year, today.year + 1):
        try:
            candidate = anchor.replace(year=year)
        except ValueError:  # Feb 29
            candidate = date(year, 2, 28)
        if candidate >= today:
            return candidate
    return anchor


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def top_opportunities(ctx: ScoringContext, limit: int = 5) -> Section:
    frame, evaluations = rank(ctx)
    section = Section("top_opportunities", "Top picks for you")
    if frame.empty:
        section.note = "No cards to rank yet (run sync, then onboard your spend)."
        return section
    eligible = frame[
        (frame["eligibility"] == "eligible")
        & (frame["hits_min_spend"].ne(False))
        & (frame["marginal_ev_year1"] >= ctx.profile.min_marginal_ev_alert)
    ]
    opened = cards_opened_last_12_months(ctx)
    if opened >= ctx.profile.max_new_cards_per_year:
        section.note = (
            f"You've opened {opened} cards in the last 12 months (your limit is "
            f"{ctx.profile.max_new_cards_per_year}); consider waiting before applying."
        )
    for row in eligible.head(limit).itertuples():
        ev = evaluations[row.card_id]
        offer = f" ({ev.offer_summary})" if ev.offer_summary else ""
        short = (
            f"{ev.name}: {money(ev.marginal_ev_year1, True)} yr 1, "
            f"{money(ev.marginal_ev_steady, True)}/yr after{offer}"
        )
        math = "\n".join(
            f"    - {line.label}: {money(line.amount, True)}"
            + (f" ({line.detail})" if line.detail else "")
            for line in ev.marginal_breakdown
            if line.amount or line.detail.startswith("already")
        )
        full = (
            f"**{ev.name}** — marginal EV {money(ev.marginal_ev_year1, True)} in year 1, "
            f"{money(ev.marginal_ev_steady, True)}/yr after; annual fee {money(ev.annual_fee)}{offer}. "
            f"vs a flat 2% card: {money(ev.vs_flat_2pct_steady, True)}/yr.\n{math}"
        )
        section.items.append(Item(short, full, ev.to_dict() | {"breakdown": None}))
    if not section.items and not section.note:
        section.note = (
            f"Nothing clears your {money(ctx.profile.min_marginal_ev_alert)} threshold this month."
        )
    return section


def bonus_changes(ctx: ScoringContext, changes: ChangeSet | None, limit: int = 5) -> Section:
    section = Section("bonus_changes", "New & elevated bonuses")
    if changes is None:
        section.note = "No changes file yet."
        return section
    held = {w.card_id for w in ctx.open_wallet}
    rows = [
        {**c, "change": "elevated", "amount": c["new_bonus"], "was": c["old_bonus"]}
        for c in changes.elevated_bonuses
    ] + [
        {**c, "change": "new offer", "amount": c["bonus_amount"], "was": None}
        for c in changes.new_offers
    ]
    rows += [
        {**c, "change": "new card", "amount": None, "was": None, "bonus_unit": None}
        for c in changes.new_cards
    ]
    for change in rows[: limit * 2]:
        card_id = change["card_id"]
        if card_id in held or card_id not in ctx.data.cards:
            continue
        card = ctx.data.cards[card_id]
        status = ctx.checker.check(card).status
        unit = change.get("bonus_unit")
        amount = ""
        if change["amount"]:
            amount = (
                f" ${change['amount']:,.0f}"
                if unit == "usd"
                else f" {change['amount']:,.0f} {unit}"
            )
        was = f" (was {change['was']:,.0f})" if change["was"] else ""
        text = f"{card.display_name}{amount}{was}, {change['change']}"
        tag = "" if status == "eligible" else f" [{status}]"
        section.items.append(Item(text + tag, text + tag, {**change, "eligibility": status}))
        if len(section.items) >= limit:
            break
    if changes.is_empty:
        section.note = f"No bonus changes since {changes.previous_date or 'the first snapshot'}."
    return section


def personal_offers_section(
    offers: list[PersonalOffer], ctx: ScoringContext, limit: int = 5
) -> Section:
    section = Section("personal_offers", "Offers in your inbox (last 31 days)")
    real = [o for o in offers if o.kind != "forwarding_confirmation"]
    phishing = [o for o in real if o.suspected_phishing]
    for offer in [o for o in real if not o.suspected_phishing][:limit]:
        card = ctx.data.cards.get(offer.card_id) if offer.card_id else None
        name = card.display_name if card else (offer.issuer or "Unknown issuer").title()
        parts = [offer.kind.replace("_", " ")]
        if offer.bonus_amount:
            unit = "$" if offer.bonus_unit and offer.bonus_unit.value == "usd" else ""
            suffix = "" if unit else f" {offer.bonus_unit.value if offer.bonus_unit else 'points'}"
            parts.append(f"{unit}{offer.bonus_amount:,.0f}{suffix}")
        if offer.min_spend:
            parts.append(f"after ${offer.min_spend:,.0f}")
        if offer.expires_at:
            parts.append(f"expires {offer.expires_at:%b %d}")
        short = f"{name}: {', '.join(parts)}"
        full = f'{short}. Subject: "{sanitize_untrusted(offer.subject, 120)}"'
        section.items.append(Item(short, full, offer.model_dump(mode="json")))
    if phishing:
        section.note = f"⚠ {len(phishing)} suspicious email(s) flagged and left out; don't click anything in them."
    elif not real:
        section.note = "None received."
    return section


def fees_due(ctx: ScoringContext, limit: int = 5) -> Section:
    section = Section("fees_due", f"Annual fees due in the next {FEE_WINDOW_DAYS} days")
    for held in ctx.open_wallet:
        card = ctx.data.cards[held.card_id]
        if not held.annual_fee_date or card.annual_fee <= 0:
            continue
        due = next_occurrence(held.annual_fee_date, ctx.today)
        if (due - ctx.today).days > FEE_WINDOW_DAYS:
            continue
        keep = evaluate(card.id, ctx).marginal_ev_steady
        if keep >= 0:
            advice = f"keep: adds {money(keep, True)}/yr net of the fee"
        else:
            options = [
                ctx.data.cards[cid].display_name
                for cid in ctx.data.downgrade_paths.get(card.id, [])
                if cid in ctx.data.cards
            ]
            target = (
                f"downgrade to {' or '.join(options[:2])}" if options else "cancel or downgrade"
            )
            advice = f"consider asking for a retention offer, else {target} ({money(keep, True)}/yr as is)"
        text = f"{card.display_name} {money(card.annual_fee)} on {due:%b %d}: {advice}"
        section.items.append(
            Item(
                text,
                text,
                {"card_id": card.id, "due": due.isoformat(), "keep_value": round(keep, 2)},
            )
        )
        if len(section.items) >= limit:
            break
    if not section.items:
        section.note = "Nothing due (add annual_fee_date to wallet cards to track this)."
    return section


def unused_credits(ctx: ScoringContext, limit: int = 5) -> Section:
    section = Section("unused_credits", "Credits you may be leaving unused")
    rows = [
        {
            "card_id": held.card_id,
            "card": ctx.data.cards[held.card_id].display_name,
            "benefit": benefit.name,
            "kind": benefit.kind.value,
            "face": benefit_face_usd(benefit, ctx)[0],
            "usage": ctx.haircuts.get(benefit.kind, 0.0),
        }
        for held in ctx.open_wallet
        for benefit in ctx.data.benefits.get(held.card_id, [])
        if benefit.kind in CREDIT_KINDS and benefit.face_value_annual > 0 and not benefit.automatic
    ]
    if rows:
        frame = pd.DataFrame(rows)
        frame["unused"] = frame["face"] * (1 - frame["usage"])
        frame = frame[frame["unused"] >= 1].sort_values("unused", ascending=False).head(limit)
        for row in frame.itertuples():
            text = (
                f"{row.card}: {row.benefit} — ~{money(row.unused)}/yr unused "
                f"(you use ~{row.usage:.0%} of {money(row.face)})"
            )
            section.items.append(
                Item(text, text, {k: getattr(row, k) for k in ("card_id", "benefit", "unused")})
            )
    if not section.items:
        section.note = "Nothing obvious."
    return section


def news_section(
    ctx: ScoringContext, relevant_ids: set[str], now: datetime, limit: int = 5
) -> Section:
    section = Section("news", "Worth a look (Doctor of Credit)")
    cutoff = now - timedelta(days=NEWS_WINDOW_DAYS)
    for item in ctx.data.snapshot.news:
        if item.published_at < cutoff or {"bank", "expired"} & set(item.tags):
            continue
        linked = bool(set(item.card_ids) & relevant_ids)
        if not (linked or item.kind in ("new_bonus", "elevated_bonus")):
            continue
        title = sanitize_untrusted(item.title, 110)
        section.items.append(
            Item(
                f'"{title}"',
                f"[{title}]({item.url}) — {item.published_at:%b %d}",
                {"title": title, "url": item.url, "kind": item.kind, "card_ids": item.card_ids},
            )
        )
        if len(section.items) >= limit:
            break
    if not section.items:
        section.note = "Nothing relevant in the last 35 days."
    return section


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_short(title: str, sections: list[Section], per_section: int, footer: str) -> str:
    lines = [f"*{title}*"]
    for section in sections:
        if not section.items and not section.note:
            continue
        lines += ["", f"*{section.title}*"]
        lines += [f"{i}. {item.short}" for i, item in enumerate(section.items[:per_section], 1)]
        if section.note and (section.key != "news" or not section.items):
            lines.append(f"_{section.note}_")
    lines += ["", footer]
    return "\n".join(lines)


def render_full(title: str, sections: list[Section], header: list[str]) -> str:
    lines = [f"# {title}", "", *header]
    for section in sections:
        lines += ["", f"## {section.title}", ""]
        lines += [f"- {item.full}" for item in section.items]
        if section.note:
            lines.append(f"_{section.note}_")
    lines += [
        "",
        "---",
        "Values are estimates from your own spend, point valuations and usage haircuts; "
        "bonus terms come from public sources and your inbox and can change. This agent never "
        "applies for cards or logs into accounts. Not financial advice.",
    ]
    return "\n".join(lines)


def build_digest(
    ctx: ScoringContext,
    changes: ChangeSet | None,
    offers: list[PersonalOffer],
    now: datetime,
    warnings: list[str] | None = None,
) -> Digest:
    top = top_opportunities(ctx)
    relevant = {w.card_id for w in ctx.open_wallet} | {i.data["card_id"] for i in top.items}
    sections = [
        top,
        bonus_changes(ctx, changes),
        personal_offers_section(offers, ctx),
        fees_due(ctx),
        unused_credits(ctx),
        news_section(ctx, relevant, now),
    ]
    title = f"Card digest · {now:%B %Y}"
    footer = 'Full breakdown in your email. Ask me to "compare X Y" or "explain X" for the math.'
    per_section = 3
    short = render_short(title, sections, per_section, footer)
    while len(short) > WHATSAPP_LIMIT and per_section > 1:
        per_section -= 1
        short = render_short(title, sections, per_section, footer)
    if len(short) > WHATSAPP_LIMIT:
        short = short[: WHATSAPP_LIMIT - 1].rstrip() + "…"

    snapshot = ctx.data.snapshot
    header = [
        f"Data as of {snapshot.generated_at:%Y-%m-%d} "
        f"({', '.join(f'{k}: {v.status}' for k, v in snapshot.sources.items())}).",
        *[f"⚠ {warning}" for warning in warnings or []],
    ]
    full = render_full(title, sections, header)
    return Digest(period=f"{now:%Y-%m}", title=title, short=short, full=full, sections=sections)
