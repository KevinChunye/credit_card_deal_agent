"""python -m card_agent <command>

Every command prints one JSON object with `ok`, a `display_text` to relay
verbatim (plain text with emoji: no Markdown, no code blocks, no JSON), and
`next`, the agent's instruction for its loop:

    {"action": "stop"}                         relay display_text; the request is done
    {"action": "ask_user", "reason": ...}      relay display_text (it ends with the
                                               question) and wait for the answer
    {"action": "run", "command": "sync", ...}  run that command once, then retry once
    {"action": "fix_command"}                  the command was malformed: fix it and
                                               retry once; don't show this to the user

The digest's --print prints its WhatsApp text instead. The JSON is also saved
to last_response.json next to the state DB (for exec environments that
swallow stdout), and each command appends a line to trace.jsonl (trace.py).
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from card_agent import digest as digest_mod
from card_agent import inbox, mailer, onboard, trace, views
from card_agent.advisor import MODE_LABEL, ONBOARDING_QUESTION, Advisor, Goal
from card_agent.config import Settings
from card_agent.credit import assess
from card_agent.eligibility import load_rules
from card_agent.freshness import terms_warning
from card_agent.guardrails import GuardrailError
from card_agent.links import apply_link, load_links, prequalify_link
from card_agent.matching import issuers_in
from card_agent.models import ISSUER_DISPLAY, Category, WalletCard
from card_agent.present import category_label, parse_category, plural
from card_agent.scoring import ScoringContext, evaluate, rank, wallet_rates
from card_agent.snapshot import (
    DataView,
    FetchFailed,
    SnapshotMissing,
    load_changes,
    load_snapshot,
    refresh,
    saved_age_days,
    snapshot_age_days,
    sync,
)
from card_agent.store import Store
from card_agent.verifier import Brief, Verifier

STALE_AFTER_DAYS = 14
# `use` with no spending on file shows these.
DEFAULT_USE_CATEGORIES = [
    Category.dining,
    Category.groceries,
    Category.gas,
    Category.flights,
    Category.hotels,
    Category.other,
]
HARD_PULL_NOTE = (
    "⚠️ Before you apply, confirm the bonus and fee on that page. An application is a hard "
    "inquiry: usually a few points off your score, counted for 12 months."
)
NEVER_APPLY = "I never apply for you; you decide and apply yourself."


def stop() -> dict[str, Any]:
    return {"action": "stop"}


def ask(reason: str) -> dict[str, Any]:
    return {"action": "ask_user", "reason": reason}


class CommandError(Exception):
    """A user-facing error: printed as {"ok": false, "error": ..., "next": ...}."""

    def __init__(self, message: str, next_step: dict[str, Any] | None = None):
        super().__init__(message)
        self.next = next_step or stop()


def money(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "–"
    return digest_mod.money(value, signed)


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------


def load_data(settings: Settings) -> DataView:
    return DataView(load_snapshot(settings))


def build_context(settings: Settings, store: Store, now: datetime) -> ScoringContext:
    return ScoringContext(
        data=load_data(settings),
        profile=store.get_profile(),
        spend=store.get_spend(),
        valuations=store.get_valuations(),
        haircuts=store.get_haircuts(),
        wallet=store.list_wallet(),
        rules=load_rules(),
        today=now.date(),
        hidden_cards=store.hidden_values("card"),
        hidden_issuers=store.hidden_values("issuer"),
    )


def resolve(query: str, data: DataView, notes: list[str] | None = None, guess: bool = True) -> str:
    """A card id for what the user typed. A clear typo is read as the card it
    obviously is (noted in `notes`), but only when `guess` is on: commands that
    write to memory never act on a guess. Anything unclear asks the user."""
    card_id, candidates = data.matcher.resolve(query)
    if card_id:
        return card_id
    if candidates:
        options = "; ".join(data.cards[c].display_name for c in candidates[:6])
        raise CommandError(
            f"🙋 {query!r} could be several cards: {options}. Which one do you mean?",
            ask("ambiguous card name"),
        )
    guessed = data.matcher.confident_guess(query) if guess else None
    if guessed:
        if notes is not None:
            notes.append(f"🔤 I read {query!r} as {data.cards[guessed].display_name}.")
        return guessed
    suggestions = [data.cards[c].display_name for c in data.matcher.suggest(query)["card_id"]]
    if suggestions:
        raise CommandError(
            f"🙋 I don't know a card called {query!r}. Did you mean {' or '.join(suggestions)}?",
            ask("unknown card name"),
        )
    raise CommandError(
        f"🙋 I don't know a card called {query!r}. Could you give its full name, like "
        '"Chase Sapphire Preferred"?',
        ask("unknown card name"),
    )


def resolve_issuer(query: str) -> str:
    slug = query.strip().lower()
    if slug in ISSUER_DISPLAY:
        return slug
    found = issuers_in(query)
    if len(found) == 1:
        return found.pop()
    raise CommandError(
        f"🙋 Which bank do you mean by {query!r}? For example Chase, Amex, Capital One or Citi.",
        ask("unknown issuer"),
    )


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def fee_limit(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a dollar amount of 0 or more")
    return value


def _staleness(settings: Settings, now: datetime) -> list[str]:
    try:
        age = snapshot_age_days(load_snapshot(settings), now)
    except SnapshotMissing:
        return []
    if age > STALE_AFTER_DAYS:
        return [f"Card data is {age:.0f} days old; the weekly collector may be failing."]
    return []


def _with_notes(notes: list[str], text: str) -> str:
    return "\n".join([*notes, text]) if notes else text


def _hidden_note(ctx: ScoringContext) -> list[str]:
    count = len(ctx.hidden_cards) + len(ctx.hidden_issuers)
    if not count:
        return []
    return [
        f"🙈 Skipping {plural(count, 'card or issuer', 'cards or issuers')} you asked me to hide "
        '(say "show hidden cards again" to undo).'
    ]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_sync(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    if args.from_file:
        summary = sync(settings, None, from_file=args.from_file)
        text = (
            f"🔄 Loaded card data from {args.from_file.name} (generated "
            f"{summary['generated_at'][:10]}): {summary['cards']} cards, {summary['offers']} "
            f"offers, {summary['news']} news items."
        )
        return {"display_text": text, "sync": summary}
    result = refresh(settings, now)
    if result.ok:
        summary = result.summary
        text = (
            f"🔄 Card data refreshed (generated {summary['generated_at'][:10]}): "
            f"{summary['cards']} cards, {summary['offers']} offers, {summary['news']} news items."
        )
        if result.attempts:
            text += f" It took {plural(len(result.attempts) + 1, 'try', 'tries')}."
    elif result.usable:
        text = (
            f"⚠️ I couldn't reach the card data server ({result.plain_reason}, "
            f"{plural(len(result.attempts), 'try', 'tries')} on two routes). I'm still using the "
            f"saved copy from {plural(round(result.saved_age_days), 'day')} ago, so everything "
            "keeps working. "
            "I'll try again next time."
        )
    else:
        raise CommandError(
            f"⚠️ I couldn't download the card data ({result.plain_reason}) and have no saved copy yet. "
            "Please try again in a few minutes.",
            ask("card data unavailable"),
        )
    return {"display_text": text, "sync": result.to_dict()}


def _saved_text(changed: dict[str, Any], matcher) -> str:
    def cards(ids: list[str]) -> str:
        if matcher is None:
            return ", ".join(ids)
        return ", ".join(matcher.cards[c].display_name if c in matcher.cards else c for c in ids)

    parts = []
    for section, keys in changed.items():
        if section == "monthly_spend":
            parts.append(
                "monthly spending (" + ", ".join(category_label(k).lower() for k in keys) + ")"
            )
        elif section == "wallet":
            parts.append(f"wallet ({cards(keys)})")
        else:
            words = ", ".join(str(k).replace("_", " ") for k in keys)
            parts.append(f"{section.replace('_', ' ')} ({words})" if words else section)
    return "; ".join(parts) or "nothing new"


def cmd_onboard(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    try:
        matcher = load_data(settings).matcher
    except SnapshotMissing:
        matcher = None
    if args.show:
        payload = cmd_memory(args, settings, store, now)
        return payload | {"setup": onboard.show(store)}
    if args.from_yaml:
        patch = yaml.safe_load(Path(args.from_yaml).expanduser().read_text()) or {}
        changed = onboard.apply_patch(store, patch, matcher)
    elif args.json:
        changed = onboard.apply_patch(store, json.loads(args.json), matcher)
    elif sys.stdin.isatty():
        changed = onboard.interactive(store, matcher)
    else:
        raise CommandError(
            "Pass --from-yaml FILE or --json '{...}' (or run in a terminal).",
            {"action": "fix_command"},
        )
    warning = (
        " (I haven't downloaded card data yet, so card names weren't checked)"
        if not matcher
        else ""
    )
    text = f"✅ Saved{warning}: {_saved_text(changed, matcher)}."
    return {"display_text": text, "changed": changed}


def cmd_wallet(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    if args.wallet_command == "list":
        wallet = store.list_wallet()
        try:
            cards = load_data(settings).cards
        except SnapshotMissing:
            cards = {}
        return {
            "display_text": views.wallet_text(wallet, cards),
            "wallet": [w.model_dump(mode="json") for w in wallet],
        }

    data = load_data(settings)
    card_id = resolve(" ".join(args.card), data, guess=False)
    name = data.cards[card_id].display_name
    if args.wallet_command == "remove":
        removed = store.remove_wallet_card(card_id)
        text = (
            f"🗑️ Removed {name} from your wallet." if removed else f"{name} wasn't in your wallet."
        )
        return {"display_text": text, "removed": removed, "card_id": card_id}

    card = WalletCard(
        card_id=card_id,
        opened_on=_date(args.opened),
        annual_fee_date=_date(args.fee_date),
        bonus_received_on=_date(args.bonus_received),
        product_changed_from=resolve(args.product_changed_from, data, guess=False)
        if args.product_changed_from
        else None,
        closed_on=_date(args.closed),
    )
    saved = store.upsert_wallet_card(card)
    return {
        "display_text": f"✅ Saved {name} in your wallet.",
        "card": saved.model_dump(mode="json"),
    }


def _scope(mode: str | None, kind: str | None, max_af: float) -> str:
    parts = [MODE_LABEL[mode]] if mode else []
    if kind == "all":
        parts.append("personal and business")
    elif kind == "business":
        parts.append("business only")
    parts.append(f"annual fee up to {money(max_af)}")
    return ", ".join(parts)


def cmd_rank(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    ctx = build_context(settings, store, now)
    if not ctx.spend:
        raise CommandError(ONBOARDING_QUESTION, ask("no spending profile yet"))
    frame, evaluations = rank(
        ctx,
        mode=args.mode,
        kind=args.kind,
        max_annual_fee=args.max_af,
        include_ineligible=args.include_ineligible,
        include_hidden=args.include_hidden,
    )
    top = frame.head(args.top)
    today = now.date()
    cap = args.max_af if args.max_af is not None else ctx.profile.max_annual_fee
    shown = [evaluations[card_id] for card_id in top["card_id"]]
    notes = [] if args.include_hidden else _hidden_note(ctx)
    text = views.rank_text(shown, ctx.data.cards, _scope(args.mode, args.kind, cap), today, notes)
    results = []
    for ev in shown:
        entry = {
            k: v for k, v in ev.to_dict().items() if k not in ("breakdown", "marginal_breakdown")
        }
        entry["terms_warning"] = terms_warning(ctx.data.cards[ev.card_id], today)
        if args.json:
            entry["breakdown"] = ev.to_dict()["breakdown"]
            entry["marginal_breakdown"] = ev.to_dict()["marginal_breakdown"]
        results.append(entry)
    return {"display_text": text, "results": results, "warnings": _staleness(settings, now)}


def cmd_explain(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    ctx = build_context(settings, store, now)
    notes: list[str] = []
    ev = evaluate(resolve(" ".join(args.card), ctx.data, notes), ctx)
    card = ctx.data.cards[ev.card_id]
    return {
        "display_text": _with_notes(notes, views.explain_text(ev, card, now.date())),
        "evaluation": ev.to_dict(),
        "terms_warning": terms_warning(card, now.date()),
    }


def cmd_compare(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    ctx = build_context(settings, store, now)
    notes: list[str] = []
    a = evaluate(resolve(args.card_a, ctx.data, notes), ctx)
    b = evaluate(resolve(args.card_b, ctx.data, notes), ctx)
    today = now.date()
    card_a, card_b = ctx.data.cards[a.card_id], ctx.data.cards[b.card_id]
    return {
        "display_text": _with_notes(notes, views.compare_text(a, b, card_a, card_b, today)),
        "a": a.to_dict() | {"terms_warning": terms_warning(card_a, today)},
        "b": b.to_dict() | {"terms_warning": terms_warning(card_b, today)},
    }


def cmd_verify(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    ctx = build_context(settings, store, now)
    notes: list[str] = []
    card_id = resolve(" ".join(args.card), ctx.data, notes)
    ev = evaluate(card_id, ctx)
    brief = Brief.for_candidate(ev, ctx, args.max_af)
    verifier = Verifier(ctx.data, ctx.rules, ctx.today, saved_age_days(settings, now))
    report = verifier.run(brief)
    card = ctx.data.cards[card_id]
    steps = [
        {"n": 1, "phase": "handoff", "text": f"Ask the Verifier to check {ev.name}.",
         "data": {"role": verifier.role, "brief": brief.to_dict()}},
        {"n": 2, "phase": "result", "text": f"Verifier on {ev.name}: {report.verdict} ({report.summary}).",
         "data": {"report": report.to_dict()}},
    ]  # fmt: skip
    return {
        "display_text": _with_notes(
            notes, views.verify_text(report, card, prequalify_link(card.issuer))
        ),
        "verdict": report.verdict,
        "report": report.to_dict(),
        "brief": brief.to_dict(),
        "_steps": steps,
    }


def cmd_advise(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    advisor = Advisor(
        store=store,
        load_context=lambda: build_context(settings, store, now),
        data_age=lambda: saved_age_days(settings, now),
        refresh=None if args.no_sync else (lambda: refresh(settings, now)),
        now=now,
    )
    outcome = advisor.run(Goal(mode=args.mode, kind=args.kind, max_af=args.max_af))
    pick = None
    prequal = None
    cards = {}
    if outcome.pick:
        cards = load_data(settings).cards
        card = cards[outcome.pick.card_id]
        prequal = prequalify_link(card.issuer)
        pick = {
            "card_id": card.id,
            "name": card.display_name,
            "annual_fee": card.annual_fee,
            "year1": round(outcome.pick.marginal_ev_year1, 2),
            "steady": round(outcome.pick.marginal_ev_steady, 2),
            "offer": outcome.pick.offer_summary,
            "apply_url": outcome.report.apply_url,
            "prequalify_url": prequal,
        }
    return {
        "display_text": views.advise_text(outcome, cards, prequal),
        "status": outcome.status,
        "pick": pick,
        "verifier": outcome.report.to_dict() if outcome.report else None,
        "rejected": [
            {"card_id": r.card_id, "reason": r.with_status("fail")[0].detail}
            for r in outcome.rejected
        ],
        # The full steps, with each handoff's brief and report, go to the trace log.
        "loop": {
            "steps": len(outcome.steps),
            "verifier_calls": outcome.verifier_calls,
            "stopped_because": outcome.reason,
            "phases": [step.phase for step in outcome.steps],
        },
        "next": stop() if outcome.status == "pick" else ask(outcome.reason),
        "_steps": [step.to_dict() for step in outcome.steps],
    }


def cmd_apply_link(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    data = load_data(settings)
    notes: list[str] = []
    card = data.cards[resolve(" ".join(args.card), data, notes)]
    if card.discontinued:
        text = f"🚫 {card.display_name} isn't taking new applications any more."
        return {"display_text": _with_notes(notes, text), "apply_url": None}
    url = apply_link(card)
    prequal = prequalify_link(card.issuer)
    lines = [f"💳 {card.display_name}"]
    lines += views.link_lines(url, prequal, views.issuer_name(card))
    lines += [HARD_PULL_NOTE, NEVER_APPLY]
    return {
        "display_text": _with_notes(notes, "\n".join(lines)),
        "apply_url": url,
        "prequalify_url": prequal,
    }


def cmd_credit(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    ctx = build_context(settings, store, now)
    health = assess(ctx)
    links = load_links()
    return {
        "display_text": views.credit_text(health, links.prequalify, links.credit_resources),
        "credit": health.to_dict(),
    }


def cmd_use(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    ctx = build_context(settings, store, now)
    if args.category:
        words = " ".join(args.category)
        category = parse_category(words)
        if category is None:
            raise CommandError(
                f"🙋 Which kind of spending is {words!r}? For example dining, groceries, gas, "
                "flights, hotels, transit, streaming or everything else.",
                ask("unknown category"),
            )
        categories = [category]
    else:
        categories = [c for c in Category if ctx.spend.get(c)] or DEFAULT_USE_CATEGORIES
    rows = []
    for category in categories:
        frame = wallet_rates(ctx, category)
        if frame.empty:
            continue
        options = frame.head(2).to_dict("records")
        rows.append(
            {
                "category": category.value,
                "best": options[0],
                "runner_up": options[1] if len(options) > 1 else None,
                "show_runner_up": bool(args.category),
            }
        )
    return {"display_text": views.use_text(rows), "use": rows}


def cmd_hide(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    if args.issuer:
        issuer = resolve_issuer(args.issuer)
        store.hide("issuer", issuer, args.reason, now)
        name = ISSUER_DISPLAY.get(issuer, issuer)
        text = (
            f"🙈 Got it: no more {name} cards in my suggestions. "
            f'Say "show {name} cards again" to undo.'
        )
        return {"display_text": text, "hidden": {"kind": "issuer", "value": issuer}}
    if not args.card:
        raise CommandError("Name a card, or pass --issuer.", {"action": "fix_command"})
    data = load_data(settings)
    card_id = resolve(" ".join(args.card), data, guess=False)
    store.hide("card", card_id, args.reason, now)
    name = data.cards[card_id].display_name
    text = f'🙈 Got it: I won\'t suggest {name} again. Say "show {name} again" to undo.'
    return {"display_text": text, "hidden": {"kind": "card", "value": card_id}}


def cmd_unhide(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    if args.all:
        rows = store.hidden()
        for row in rows:
            store.unhide(row["kind"], row["value"])
        text = f"👀 Showing everything again ({plural(len(rows), 'hidden item')} cleared)."
        return {"display_text": text, "cleared": len(rows)}
    if args.issuer:
        kind, value = "issuer", resolve_issuer(args.issuer)
        name = f"{ISSUER_DISPLAY.get(value, value)} cards"
    elif args.card:
        data = load_data(settings)
        kind, value = "card", resolve(" ".join(args.card), data, guess=False)
        name = data.cards[value].display_name
    else:
        raise CommandError("Name a card, or pass --issuer or --all.", {"action": "fix_command"})
    removed = store.unhide(kind, value)
    text = (
        f"👀 {name} can show up in my suggestions again." if removed else f"{name} wasn't hidden."
    )
    return {"display_text": text, "removed": removed}


def cmd_memory(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    try:
        cards = load_data(settings).cards
    except SnapshotMissing:
        cards = {}
    hidden = store.hidden()
    recommendations = store.recommendations(limit=3)
    text = views.memory_text(
        store.get_profile(),
        store.get_spend(),
        store.list_wallet(),
        hidden,
        recommendations,
        store.custom_valuations(),
        store.counts(),
        store.last_digest("email") or store.last_digest("whatsapp"),
        cards,
    )
    return {
        "display_text": text,
        "memory": onboard.show(store) | {"hidden": hidden, "recommendations": recommendations},
    }


def cmd_trace(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    records = trace.read(settings.state_dir, last=args.last)
    # Briefs and reports stay in the log file; the chat model only needs the steps.
    brief = [
        entry | {"steps": [trace.without_data(step) for step in entry.get("steps") or []]}
        for entry in records
    ]
    return {"display_text": views.trace_text(records), "records": brief}


def cmd_digest(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    warnings: list[str] = []
    if not args.no_sync:
        refreshed = refresh(settings, now)
        if not refreshed.ok:
            warnings.append(
                f"Couldn't refresh card data ({refreshed.plain_reason}); using the saved copy."
            )
    warnings += _staleness(settings, now)
    ctx = build_context(settings, store, now)
    offers = store.personal_offers(since=now - timedelta(days=31))
    result = digest_mod.build_digest(ctx, load_changes(settings), offers, now, warnings)
    try:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        (settings.state_dir / "digest_latest.html").write_text(result.html, encoding="utf-8")
    except OSError:
        pass
    payload: dict[str, Any] = {
        "email_text": result.full,
        "digest": result.to_dict(),
        "warnings": warnings,
    }
    emailed = False
    if args.send_email:
        try:
            sent = mailer.send_digest(
                settings,
                f"Your card digest — {now:%B %Y}",
                result.full,
                result.period,
                html=result.html,
            )
            store.log_digest(now, "email", result.period, sent)
            payload["email"] = {"sent": True, **sent}
            emailed = True
        except (GuardrailError, inbox.InboxNotConfigured, ValueError) as exc:
            payload["email"] = {"sent": False, "error": str(exc)}
        except Exception as exc:  # network/API errors must not lose the WhatsApp text
            payload["email"] = {"sent": False, "error": f"{type(exc).__name__}: {exc}"}
    # Only point to the email when it went out.
    short = result.short if emailed else result.short.replace(digest_mod.EMAIL_NOTE, "")
    payload["display_text"] = short
    store.log_digest(now, "whatsapp", result.period, {"chars": len(short)})
    if args.print:
        payload["_print"] = short
    return payload


def cmd_inbox(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    result = inbox.poll(settings, store, load_data(settings), now)
    lines = [
        f"📬 Checked {plural(result['checked'], 'message')}: "
        f"{plural(len(result['accepted']), 'new offer')}, {len(result['rejected'])} rejected, "
        f"{result['already_seen']} already seen."
    ]
    for offer in result["accepted"]:
        if offer["kind"] == "forwarding_confirmation":
            continue
        flag = (
            " 🚩 suspected phishing, don't click anything in it"
            if offer["suspected_phishing"]
            else ""
        )
        amount = (
            f" {offer['bonus_amount']:,.0f} {offer['bonus_unit']}"
            if offer.get("bonus_amount")
            else ""
        )
        issuer = (offer["issuer"] or "unknown sender").title()
        lines.append(f"• {issuer} {offer['kind'].replace('_', ' ')}{amount}{flag}")
    for item in result["action_required"]:
        lines.append(
            f"🙋 Action needed: Gmail forwarding confirmation code {item['confirmation_code']}. "
            "Enter it in Gmail > Settings > Forwarding and POP/IMAP. I won't click the link for you."
        )
    return {"display_text": "\n".join(lines), **result}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class JsonErrorParser(argparse.ArgumentParser):
    """Usage errors become {"ok": false, ...} JSON instead of stderr text."""

    def error(self, message: str):
        raise CommandError(f"{message}\n{self.format_usage().strip()}", {"action": "fix_command"})


def build_parser() -> argparse.ArgumentParser:
    parser = JsonErrorParser(prog="python -m card_agent", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="pull the latest snapshot from the data branch")
    p.add_argument("--from-file", type=Path, help="load a local data/latest.json instead")
    p.set_defaults(handler=cmd_sync)

    p = sub.add_parser("onboard", help="set up profile, spend, valuations, haircuts, wallet")
    p.add_argument("--from-yaml", help="YAML file (see config/user_profile.example.yaml)")
    p.add_argument("--json", help="partial setup as JSON, merged into what's stored")
    p.add_argument("--show", action="store_true", help="print the current setup")
    p.set_defaults(handler=cmd_onboard)

    p = sub.add_parser("wallet", help="add, remove or list cards you hold")
    wallet = p.add_subparsers(dest="wallet_command", required=True)
    add = wallet.add_parser("add")
    add.add_argument("card", nargs="+")
    add.add_argument("--opened", help="YYYY-MM-DD")
    add.add_argument("--fee-date", help="YYYY-MM-DD the annual fee posts")
    add.add_argument("--bonus-received", help="YYYY-MM-DD")
    add.add_argument("--product-changed-from", help="card you product-changed from")
    add.add_argument("--closed", help="YYYY-MM-DD (keeps history for issuer rules)")
    remove = wallet.add_parser("remove")
    remove.add_argument("card", nargs="+")
    wallet.add_parser("list")
    p.set_defaults(handler=cmd_wallet)

    p = sub.add_parser("advise", help="pick one card for you, checked by the Verifier")
    p.add_argument("--mode", choices=["travel", "cash_back", "business"])
    p.add_argument("--kind", choices=["personal", "business", "all"])
    p.add_argument("--max-af", type=fee_limit, help="max annual fee (default: your profile)")
    p.add_argument("--no-sync", action="store_true", help="don't refresh stale card data")
    p.set_defaults(handler=cmd_advise)

    p = sub.add_parser("rank", help="rank cards by marginal value to you")
    p.add_argument("--mode", choices=["travel", "cash_back", "business"])
    p.add_argument("--kind", choices=["personal", "business", "all"])
    p.add_argument("--max-af", type=fee_limit, help="max annual fee (default: your profile)")
    p.add_argument("--top", type=positive_int, default=5)
    p.add_argument("--include-ineligible", action="store_true")
    p.add_argument("--include-hidden", action="store_true", help="show cards you hid too")
    p.add_argument("--json", action="store_true", help="include each card's itemized breakdown")
    p.set_defaults(handler=cmd_rank)

    p = sub.add_parser("verify", help="the Verifier's checks on one card")
    p.add_argument("card", nargs="+")
    p.add_argument("--max-af", type=fee_limit, help="fee limit to check against (default: profile)")
    p.set_defaults(handler=cmd_verify)

    p = sub.add_parser("compare", help="compare two cards side by side")
    p.add_argument("card_a")
    p.add_argument("card_b")
    p.set_defaults(handler=cmd_compare)

    p = sub.add_parser("explain", help="itemized math for one card")
    p.add_argument("card", nargs="+")
    p.set_defaults(handler=cmd_explain)

    p = sub.add_parser("apply-link", help="the issuer's own page for a card (never applies)")
    p.add_argument("card", nargs="+")
    p.set_defaults(handler=cmd_apply_link)

    p = sub.add_parser("credit", help="credit-health check and tips")
    p.set_defaults(handler=cmd_credit)

    p = sub.add_parser("use", help="which of your cards to use for a kind of spending")
    p.add_argument("category", nargs="*", help='e.g. "groceries", "uber"; omit for all')
    p.set_defaults(handler=cmd_use)

    p = sub.add_parser("hide", help="stop suggesting a card or an issuer")
    p.add_argument("card", nargs="*")
    p.add_argument("--issuer", help="hide every card from this bank")
    p.add_argument("--reason", help="why, in your words (kept in memory)")
    p.set_defaults(handler=cmd_hide)

    p = sub.add_parser("unhide", help="undo hide")
    p.add_argument("card", nargs="*")
    p.add_argument("--issuer")
    p.add_argument("--all", action="store_true")
    p.set_defaults(handler=cmd_unhide)

    p = sub.add_parser("memory", help="what the agent remembers about you")
    p.set_defaults(handler=cmd_memory)

    p = sub.add_parser("trace", help="the agent's recent steps, including advise's loop")
    p.add_argument("--last", type=positive_int, default=3, help="how many requests to show")
    p.set_defaults(handler=cmd_trace)

    p = sub.add_parser("digest", help="build the monthly digest")
    p.add_argument("--send-email", action="store_true", help="email the full digest to you")
    p.add_argument("--print", action="store_true", help="print the WhatsApp text only")
    p.add_argument("--no-sync", action="store_true", help="don't refresh card data first")
    p.set_defaults(handler=cmd_digest)

    p = sub.add_parser("inbox", help="personal offers from your AgentMail inbox")
    inbox_sub = p.add_subparsers(dest="inbox_command", required=True)
    inbox_sub.add_parser("poll")
    p.set_defaults(handler=cmd_inbox)
    return parser


def emit(payload: dict[str, Any], settings: Settings) -> None:
    raw = payload.pop("_print", None)
    body = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    try:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        settings.last_response_path.write_text(body)
    except OSError:
        pass
    print(raw if raw is not None else body)


def error_payload(command: str | None, exc: Exception) -> dict[str, Any]:
    """{"ok": false} with a readable message and what the agent should do next."""
    if isinstance(exc, CommandError):
        message, next_step = str(exc), exc.next
    elif isinstance(exc, SnapshotMissing):
        message = f"📭 {exc}"
        next_step = {"action": "run", "command": "sync", "then": "retry", "reason": "no card data"}
    elif isinstance(exc, GuardrailError):
        message, next_step = f"🛡️ {exc}", stop()
    elif isinstance(exc, FetchFailed | httpx.HTTPError):
        message = f"⚠️ I couldn't reach the card data server ({type(exc).__name__}). Try again soon."
        next_step = ask("network")
    elif isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
        message = "⏳ Your saved data is busy (another task is saving). Try again in a minute."
        next_step = stop()
    elif isinstance(exc, ValueError | OSError | CommandError | inbox.InboxNotConfigured):
        message, next_step = str(exc), stop()
    else:
        message = f"⚠️ That didn't work ({type(exc).__name__}: {exc})."
        next_step = stop()
    return {
        "ok": False,
        "command": command,
        "error": message,
        "display_text": message,
        "next": next_step,
    }


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    settings = Settings.from_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    started = time.perf_counter()
    now = now or datetime.now(UTC)
    steps = None
    try:
        args = build_parser().parse_args(argv)
    except CommandError as exc:
        payload = error_payload(None, exc)
        emit(payload, settings)
        trace.record(settings.state_dir, now, argv, payload, (time.perf_counter() - started) * 1000)
        return 2
    try:
        store = Store(settings.db_path)
    except Exception as exc:  # e.g. unwritable CARD_AGENT_DB, or not a database
        message = f"⚠️ I can't open your saved data ({settings.db_path}): {exc}"
        payload = {
            "ok": False,
            "command": args.command,
            "error": message,
            "display_text": message,
            "next": stop(),
        }
        emit(payload, settings)
        trace.record(settings.state_dir, now, argv, payload, (time.perf_counter() - started) * 1000)
        return 1
    try:
        payload = args.handler(args, settings, store, now)
        steps = payload.pop("_steps", None)
        payload = {"ok": True, "command": args.command, **payload}
        payload["next"] = payload.get("next") or stop()
        code = 0
    except (
        CommandError,
        SnapshotMissing,
        FetchFailed,
        GuardrailError,
        inbox.InboxNotConfigured,
        ValueError,
        OSError,
        httpx.HTTPError,
    ) as exc:
        payload = error_payload(args.command, exc)
        code = 1
    except Exception as exc:  # never a bare traceback: the agent needs JSON to act on
        payload = error_payload(args.command, exc)
        code = 1
    finally:
        store.close()
    emit(payload, settings)
    trace.record(
        settings.state_dir, now, argv, payload, (time.perf_counter() - started) * 1000, steps
    )
    return code
