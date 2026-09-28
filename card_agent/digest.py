"""Monthly digest: a short WhatsApp version and a full email version.

Sections: top marginal-EV opportunities you're eligible for, new/elevated
bonuses from the latest changes file, personal offers from your inbox,
annual fees due in the next 60 days (keep/downgrade), credits you're likely
leaving unused, and relevant Doctor of Credit news.

Both versions are plain text with emoji, so they read cleanly in any chat
app or mail client; the email also gets an HTML part with a bar chart.

Email and scraped text is untrusted: the short version never includes raw
email subjects, every scraped string goes through sanitize_untrusted, and
everything in the HTML part is escaped.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from card_agent.freshness import MARKER, data_health, terms_warning
from card_agent.guardrails import sanitize_untrusted
from card_agent.models import BenefitKind, ChangeSet, PersonalOffer
from card_agent.present import bar_lines
from card_agent.scoring import (
    ScoringContext,
    benefit_face_usd,
    cards_opened_last_12_months,
    evaluate,
    rank,
)

WHATSAPP_LIMIT = 1500
# Dropped from the WhatsApp text unless the email actually went out.
EMAIL_NOTE = "Full breakdown in your email. "
FEE_WINDOW_DAYS = 60
NEWS_WINDOW_DAYS = 35
# Benefits that are credits you can fail to use (lounge access and status aren't).
CREDIT_KINDS = set(BenefitKind) - {BenefitKind.lounge, BenefitKind.elite_status}


@dataclass
class Item:
    short: str  # one line in the chat version
    text: str  # the line in the email
    data: dict = field(default_factory=dict)
    details: list[str] = field(default_factory=list)  # indented lines under it in the email
    url: str | None = None  # written out in the text email, linked in the HTML one
    name: str | None = None  # bold at the start of the HTML line
    value: float | None = None  # drawn as a bar in the email chart


@dataclass
class Section:
    key: str
    title: str
    emoji: str
    items: list[Item] = field(default_factory=list)
    note: str | None = None

    @property
    def heading(self) -> str:
        return f"{self.emoji} {self.title}"


@dataclass
class Digest:
    period: str
    title: str
    short: str
    full: str  # plain-text email
    html: str  # HTML email
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
    section = Section("top_opportunities", "Top picks for you", "🏆")
    if frame.empty:
        section.note = "No cards to rank yet: I need card data and your spending first."
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
        warning = terms_warning(ctx.data.cards[row.card_id], ctx.today)
        short = (
            f"{ev.name}: {money(ev.marginal_ev_year1, True)} yr 1, "
            f"{money(ev.marginal_ev_steady, True)}/yr after{offer}"
            + (f" {MARKER}" if warning else "")
        )
        math = [
            f"{line.label}: {money(line.amount, True)}"
            + (f" ({line.detail})" if line.detail else "")
            for line in ev.marginal_breakdown
            if line.amount or line.detail.startswith("already")
        ]
        text = (
            f"{ev.name}: {money(ev.marginal_ev_year1, True)} in year 1 on top of your cards, "
            f"{money(ev.marginal_ev_steady, True)}/yr after; annual fee {money(ev.annual_fee)}{offer}. "
            f"Versus a flat 2% card: {money(ev.vs_flat_2pct_steady, True)}/yr."
            + (f" {MARKER} {warning}." if warning else "")
        )
        data = ev.to_dict() | {"breakdown": None, "terms_warning": warning}
        section.items.append(
            Item(short, text, data, details=math, name=ev.name, value=ev.marginal_ev_year1)
        )
    if not section.items and not section.note:
        section.note = (
            f"Nothing clears your {money(ctx.profile.min_marginal_ev_alert)} threshold this month."
        )
    return section


def bonus_changes(ctx: ScoringContext, changes: ChangeSet | None, limit: int = 5) -> Section:
    section = Section("bonus_changes", "New & elevated bonuses", "🆕")
    if changes is None:
        section.note = "No bonus changes recorded yet."
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
    for change in rows:
        card_id = change["card_id"]
        if (
            card_id in held
            or card_id not in ctx.data.cards
            or ctx.data.cards[card_id].discontinued
            or ctx.is_hidden(card_id)
        ):
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
        # What the deal is worth to you, given your wallet and spending.
        ev = evaluate(card_id, ctx)
        worth = f": {money(ev.marginal_ev_year1, True)} yr 1 for you"
        details = [
            f"Worth to you: {money(ev.marginal_ev_year1, True)} in year 1, then "
            f"{money(ev.marginal_ev_steady, True)}/yr (on top of your cards; annual fee "
            f"{money(ev.annual_fee)})"
        ]
        if ev.offer_summary:
            details.append(f"Offer: {ev.offer_summary}")
        if ev.hits_min_spend is False:
            details.append("Minimum spend is above your usual spending: don't chase it.")
        details += [
            f"{line.label}: {money(line.amount, True)}"
            + (f" ({line.detail})" if line.detail else "")
            for line in ev.marginal_breakdown
            if line.amount
        ]
        warning = terms_warning(card, ctx.today)
        if warning:
            details.append(f"{MARKER} {warning}")
        data = {**change, "eligibility": status, "year1_for_you": round(ev.marginal_ev_year1, 2)}
        section.items.append(Item(text + worth + tag, text + worth + tag, data, details=details))
        if len(section.items) >= limit:
            break
    if changes.is_empty:
        section.note = f"No bonus changes since {changes.previous_date or 'the first snapshot'}."
    elif not section.items:
        section.note = "Nothing new for cards you don't already have."
    return section


def personal_offers_section(
    offers: list[PersonalOffer], ctx: ScoringContext, limit: int = 5
) -> Section:
    section = Section("personal_offers", "Offers in your inbox (last 31 days)", "📬")
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
        text = f'{short}. Subject: "{sanitize_untrusted(offer.subject, 120)}"'
        section.items.append(Item(short, text, offer.model_dump(mode="json")))
    if phishing:
        section.note = f"🚩 {len(phishing)} suspicious email(s) flagged and left out; don't click anything in them."
    elif not real:
        section.note = "None received."
    return section


def fees_due(ctx: ScoringContext, limit: int = 5) -> Section:
    section = Section("fees_due", f"Annual fees due in the next {FEE_WINDOW_DAYS} days", "📅")
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
        section.note = (
            "Nothing due. Tell me when each card's annual fee posts and I'll warn you ahead."
        )
    return section


def unused_credits(ctx: ScoringContext, limit: int = 5) -> Section:
    section = Section("unused_credits", "Credits you may be leaving unused", "🎟️")
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
    section = Section("news", "Worth a look (Doctor of Credit)", "🗞️")
    cutoff = now - timedelta(days=NEWS_WINDOW_DAYS)
    for item in ctx.data.snapshot.news:
        if item.published_at < cutoff or {"bank", "expired"} & set(item.tags):
            continue
        linked = bool(set(item.card_ids) & relevant_ids)
        if not (linked or item.kind in ("new_bonus", "elevated_bonus")):
            continue
        title = sanitize_untrusted(item.title, 110)
        url = item.url if item.url.startswith(("https://", "http://")) else None
        section.items.append(
            Item(
                f'"{title}"',
                f"{title} ({item.published_at:%b %d})",
                {"title": title, "url": item.url, "kind": item.kind, "card_ids": item.card_ids},
                url=url,
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


DISCLAIMER = (
    "Values are estimates from your own spend, point valuations and usage haircuts; bonus "
    "terms come from public sources and your inbox and can change. This agent never applies "
    "for cards or logs into accounts. Not financial advice."
)


def render_short(
    title: str, sections: list[Section], per_section: int, footer: str, health: str = ""
) -> str:
    lines = [f"🗓️ {title}"]
    for section in sections:
        if not section.items and not section.note:
            continue
        lines += ["", section.heading]
        lines += [f"{i}. {item.short}" for i, item in enumerate(section.items[:per_section], 1)]
        if section.note and (section.key != "news" or not section.items):
            lines.append(section.note)
    if any(MARKER in item.short for section in sections for item in section.items):
        lines += ["", f"{MARKER} = card terms not verified in the last 60 days"]
    lines += ["", f"🩺 {health}", footer] if health else ["", footer]
    return "\n".join(lines)


def chart_lines(sections: list[Section]) -> list[str]:
    """Year-1 value of the top picks as text bars (the email's chart)."""
    rows = [
        (item.value, f"{money(item.value, True)} {item.name}")
        for section in sections
        for item in section.items
        if item.value is not None and item.name
    ]
    return bar_lines(rows) if rows else []


def render_full(
    title: str, sections: list[Section], header: list[str], appendix: list[str] | None = None
) -> str:
    """The plain-text email: readable as is, no markup."""
    lines = [f"🗓️ {title}", "", *header]
    chart = chart_lines(sections)
    if chart:
        lines += ["", "📊 Year 1 value of your top picks", *chart]
    for section in sections:
        lines += ["", section.heading]
        for item in section.items:
            lines.append(f"• {item.text}")
            lines += [f"    ◦ {detail}" for detail in item.details]
            if item.url:
                lines.append(f"    {item.url}")
        if section.note:
            lines.append(section.note)
    lines += appendix or []
    lines += ["", "—", DISCLAIMER]
    return "\n".join(lines)


def _html_text(item: Item) -> str:
    text = html.escape(item.text)
    if item.name and item.text.startswith(item.name):
        text = f"<b>{html.escape(item.name)}</b>{html.escape(item.text[len(item.name) :])}"
    if item.url:
        text += f' <a href="{html.escape(item.url, quote=True)}">read</a>'
    return text


def render_html(
    title: str, sections: list[Section], header: list[str], appendix: list[str] | None = None
) -> str:
    """The HTML email: the same content, with headings, lists and a bar chart.
    Every string is escaped; links are only ever http(s)."""
    esc = html.escape
    out = [
        '<!doctype html><html><body style="margin:0;padding:16px;background:#f6f7f9">',
        '<div style="max-width:640px;margin:auto;background:#ffffff;padding:20px;border-radius:12px;'
        "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937;"
        'line-height:1.5">',
        f'<h1 style="font-size:22px;margin:0 0 8px">🗓️ {esc(title)}</h1>',
    ]
    out += [f'<p style="margin:4px 0;color:#4b5563">{esc(line)}</p>' for line in header]
    rows = [
        item
        for section in sections
        for item in section.items
        if item.value is not None and item.name
    ]
    if rows:
        top = max(max(item.value for item in rows), 1.0)
        out.append(
            '<h2 style="font-size:17px;margin:20px 0 8px">📊 Year 1 value of your top picks</h2>'
        )
        out.append('<table role="presentation" style="width:100%;border-collapse:collapse">')
        for item in rows:
            width = max(0.0, min(item.value, top)) / top * 100
            out.append(
                "<tr>"
                f'<td style="padding:3px 8px 3px 0;font-size:14px">{esc(item.name)}</td>'
                '<td style="width:45%"><div style="background:#2563eb;height:12px;border-radius:3px;'
                f'width:{width:.0f}%"></div></td>'
                f'<td style="padding-left:8px;font-size:14px;white-space:nowrap">'
                f"{esc(money(item.value, True))}</td>"
                "</tr>"
            )
        out.append("</table>")
    for section in sections:
        out.append(f'<h2 style="font-size:17px;margin:20px 0 8px">{esc(section.heading)}</h2>')
        if section.items:
            out.append('<ul style="padding-left:20px;margin:0">')
            for item in section.items:
                details = ""
                if item.details:
                    details = (
                        '<ul style="color:#4b5563;font-size:13px">'
                        + "".join(f"<li>{esc(detail)}</li>" for detail in item.details)
                        + "</ul>"
                    )
                out.append(f'<li style="margin:4px 0">{_html_text(item)}{details}</li>')
            out.append("</ul>")
        if section.note:
            out.append(f'<p style="margin:4px 0;color:#6b7280">{esc(section.note)}</p>')
    for line in appendix or []:
        if line.strip():
            out.append(f'<p style="margin:4px 0;color:#4b5563;font-size:13px">{esc(line)}</p>')
    out.append(f'<p style="margin-top:24px;color:#6b7280;font-size:12px">{esc(DISCLAIMER)}</p>')
    out.append("</div></body></html>")
    return "\n".join(out)


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
    footer = "💬 " + EMAIL_NOTE + 'Ask me to "compare X and Y" or "explain X" for the math.'
    health = data_health(list(ctx.data.cards.values()), now.date())
    per_section = 3
    short = render_short(title, sections, per_section, footer, health.line)
    while len(short) > WHATSAPP_LIMIT and per_section > 1:
        per_section -= 1
        short = render_short(title, sections, per_section, footer, health.line)
    if len(short) > WHATSAPP_LIMIT:
        short = short[: WHATSAPP_LIMIT - 1].rstrip() + "…"

    snapshot = ctx.data.snapshot
    header = [
        f"Data as of {snapshot.generated_at:%Y-%m-%d} "
        f"({', '.join(f'{k}: {v.status}' for k, v in snapshot.sources.items())}).",
        f"🩺 {health.line} (Card terms are re-read from issuer pages monthly; "
        f"{MARKER} marks cards not verified in the last 60 days.)",
        *[f"⚠ {warning}" for warning in warnings or []],
    ]
    appendix = []
    if health.stale:
        appendix = ["", "🩺 Data health", health.line]
        appendix += [f"• {name}: {reason}" for name, reason in health.stale]
    full = render_full(title, sections, header, appendix)
    html_body = render_html(title, sections, header, appendix)
    return Digest(
        period=f"{now:%Y-%m}",
        title=title,
        short=short,
        full=full,
        html=html_body,
        sections=sections,
    )
