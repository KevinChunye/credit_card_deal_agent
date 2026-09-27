"""python -m card_agent <command>

Every command prints one JSON object with `ok` and a `display_text` meant to
be relayed verbatim (the digest's --print prints its markdown instead). The
same output is saved to last_response.json next to the state DB, for exec
environments that swallow stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from card_agent import digest as digest_mod
from card_agent import inbox, mailer, onboard
from card_agent.config import Settings
from card_agent.eligibility import load_rules
from card_agent.freshness import MARKER, flag, terms_warning, verified_label
from card_agent.guardrails import GuardrailError
from card_agent.models import WalletCard
from card_agent.scoring import CardEvaluation, ScoringContext, evaluate, rank
from card_agent.snapshot import (
    DataView,
    SnapshotMissing,
    load_changes,
    load_snapshot,
    snapshot_age_days,
    sync,
)
from card_agent.store import Store

STALE_AFTER_DAYS = 14


class CommandError(Exception):
    """A user-facing error; printed as {"ok": false, "error": ...}."""


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
    )


def resolve(query: str, data: DataView) -> str:
    card_id, candidates = data.matcher.resolve(query)
    if card_id:
        return card_id
    if candidates:
        options = ", ".join(f"{c} ({data.cards[c].display_name})" for c in candidates[:8])
        raise CommandError(f"{query!r} matches several cards: {options}. Use an exact id.")
    raise CommandError(f"No card matches {query!r}.")


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _staleness(settings: Settings, now: datetime) -> list[str]:
    try:
        age = snapshot_age_days(load_snapshot(settings), now)
    except SnapshotMissing:
        return []
    if age > STALE_AFTER_DAYS:
        return [f"Card data is {age:.0f} days old; the weekly collector may be failing."]
    return []


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_sync(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        summary = sync(settings, client, from_file=args.from_file)
    text = (
        f"Synced card data from {summary['origin']} (generated {summary['generated_at'][:10]}): "
        f"{summary['cards']} cards, {summary['offers']} offers, {summary['news']} news items."
    )
    return {"display_text": text, "sync": summary}


def cmd_onboard(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    try:
        matcher = load_data(settings).matcher
    except SnapshotMissing:
        matcher = None
    if args.show:
        current = onboard.show(store)
        return {"display_text": "Your current setup (private, stored locally).", "setup": current}
    if args.from_yaml:
        patch = yaml.safe_load(Path(args.from_yaml).expanduser().read_text()) or {}
        changed = onboard.apply_patch(store, patch, matcher)
    elif args.json:
        changed = onboard.apply_patch(store, json.loads(args.json), matcher)
    elif sys.stdin.isatty():
        changed = onboard.interactive(store, matcher)
    else:
        raise CommandError("Pass --from-yaml FILE or --json '{...}' (or run in a terminal).")
    warning = "" if matcher else " (no snapshot yet, so card names weren't checked; run sync)"
    parts = [
        f"{section}: {', '.join(map(str, keys)) or 'updated'}" for section, keys in changed.items()
    ]
    return {"display_text": f"Saved{warning}. " + "; ".join(parts), "changed": changed}


def cmd_wallet(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    if args.wallet_command == "list":
        wallet = store.list_wallet()
        try:
            cards = load_data(settings).cards
        except SnapshotMissing:
            cards = {}
        lines = []
        for w in wallet:
            name = cards[w.card_id].display_name if w.card_id in cards else w.card_id
            bits = [f"opened {w.opened_on}" if w.opened_on else None,
                    f"fee date {w.annual_fee_date:%b %d}" if w.annual_fee_date else None,
                    f"closed {w.closed_on}" if w.closed_on else None]  # fmt: skip
            detail = ", ".join(b for b in bits if b)
            lines.append(f"• {name}" + (f" ({detail})" if detail else ""))
        text = "Your wallet:\n" + "\n".join(lines) if lines else "Your wallet is empty."
        return {"display_text": text, "wallet": [w.model_dump(mode="json") for w in wallet]}

    data = load_data(settings)
    card_id = resolve(args.card, data)
    name = data.cards[card_id].display_name
    if args.wallet_command == "remove":
        removed = store.remove_wallet_card(card_id)
        text = f"Removed {name}." if removed else f"{name} wasn't in your wallet."
        return {"display_text": text, "removed": removed, "card_id": card_id}

    card = WalletCard(
        card_id=card_id,
        opened_on=_date(args.opened),
        annual_fee_date=_date(args.fee_date),
        bonus_received_on=_date(args.bonus_received),
        product_changed_from=resolve(args.product_changed_from, data)
        if args.product_changed_from
        else None,
        closed_on=_date(args.closed),
    )
    store.upsert_wallet_card(card)
    return {"display_text": f"Added {name} to your wallet.", "card": card.model_dump(mode="json")}


def _rank_line(i: int, row, terms_flag: str = "") -> str:
    offer = f" — {row.offer_summary}" if row.offer_summary else ""
    flags = []
    if row.eligibility != "eligible":
        flags.append(row.eligibility)
    if row.hits_min_spend is False:
        flags.append("min spend above your usual spend")
    if terms_flag:
        flags.append(terms_flag)
    flag = f" [{'; '.join(flags)}]" if flags else ""
    return (
        f"{i}. {row.name} ({money(row.annual_fee)} fee): {money(row.marginal_ev_year1, True)} year 1, "
        f"{money(row.marginal_ev_steady, True)}/yr after{offer}{flag}"
    )


def cmd_rank(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    ctx = build_context(settings, store, now)
    if not ctx.spend:
        raise CommandError("No spending profile yet. Run onboard first.")
    frame, evaluations = rank(
        ctx,
        mode=args.mode,
        kind=args.kind,
        max_annual_fee=args.max_af,
        include_ineligible=args.include_ineligible,
    )
    top = frame.head(args.top)
    scope = ", ".join(
        filter(
            None,
            [
                args.mode,
                args.kind,
                f"fee ≤ {money(args.max_af if args.max_af is not None else ctx.profile.max_annual_fee)}",
            ],
        )
    )
    today = now.date()
    flags = {card_id: flag(ctx.data.cards[card_id], today) for card_id in top["card_id"]}
    lines = [f"Top {len(top)} cards for you ({scope}). Values are vs your current wallet:"]
    lines += [_rank_line(i, row, flags[row.card_id]) for i, row in enumerate(top.itertuples(), 1)]
    if any(flags.values()):
        lines.append(
            f"{MARKER} = terms not verified against the issuer's page in the last 60 days; "
            "check the fee and earn rates before relying on them."
        )
    lines += ['Ask "explain <card>" for the itemized math.']
    results = []
    for row in top.itertuples():
        ev = evaluations[row.card_id]
        entry = {
            k: v for k, v in ev.to_dict().items() if k not in ("breakdown", "marginal_breakdown")
        }
        entry["terms_warning"] = terms_warning(ctx.data.cards[row.card_id], today)
        if args.json:
            entry["breakdown"] = ev.to_dict()["breakdown"]
            entry["marginal_breakdown"] = ev.to_dict()["marginal_breakdown"]
        results.append(entry)
    return {
        "display_text": "\n".join(lines),
        "results": results,
        "warnings": _staleness(settings, now),
    }


def _explain_text(ev: CardEvaluation) -> str:
    lines = [
        f"{ev.name}: {money(ev.year1_ev, True)} year 1 on its own, {money(ev.steady_ev, True)}/yr after."
    ]
    lines += [
        f"  {line.label}: {money(line.amount, True)} ({line.detail})"
        if line.detail
        else f"  {line.label}: {money(line.amount, True)}"
        for line in ev.breakdown
    ]
    lines += [
        f"Versus your wallet: {money(ev.marginal_ev_year1, True)} year 1, {money(ev.marginal_ev_steady, True)}/yr after."
    ]
    lines += [
        f"  {line.label}: {money(line.amount, True)} ({line.detail})"
        for line in ev.marginal_breakdown
        if line.label.startswith(("Earn", "Benefit"))
    ]
    lines += [
        f"vs a flat 2% card: {money(ev.vs_flat_2pct_steady, True)}/yr. Eligibility: {ev.eligibility}."
    ]
    lines += [f"  {reason}" for reason in ev.eligibility_reasons]
    lines += [f"Note: {note}" for note in ev.notes]
    return "\n".join(lines)  # fmt: skip


def _terms_line(card, today: date) -> str:
    warning = terms_warning(card, today)
    if warning:
        return f"{MARKER} {warning}."
    return f"Terms verified {card.last_verified:%Y-%m-%d} against {card.source_url}."


def cmd_explain(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    ctx = build_context(settings, store, now)
    ev = evaluate(resolve(args.card, ctx.data), ctx)
    card = ctx.data.cards[ev.card_id]
    return {
        "display_text": _explain_text(ev) + "\n" + _terms_line(card, now.date()),
        "evaluation": ev.to_dict(),
        "terms_warning": terms_warning(card, now.date()),
    }


def cmd_compare(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    ctx = build_context(settings, store, now)
    a = evaluate(resolve(args.card_a, ctx.data), ctx)
    b = evaluate(resolve(args.card_b, ctx.data), ctx)
    today = now.date()
    card_a, card_b = ctx.data.cards[a.card_id], ctx.data.cards[b.card_id]
    rows = [
        ("Annual fee", money(a.annual_fee), money(b.annual_fee)),
        ("Bonus value", money(a.bonus_value), money(b.bonus_value)),
        ("Earn / yr", money(a.earn_value), money(b.earn_value)),
        ("Credits / yr", money(a.benefits_value_steady), money(b.benefits_value_steady)),
        ("Year 1 EV", money(a.year1_ev, True), money(b.year1_ev, True)),
        ("Steady EV / yr", money(a.steady_ev, True), money(b.steady_ev, True)),
        ("Marginal yr 1", money(a.marginal_ev_year1, True), money(b.marginal_ev_year1, True)),
        ("Marginal / yr", money(a.marginal_ev_steady, True), money(b.marginal_ev_steady, True)),
        ("vs flat 2% / yr", money(a.vs_flat_2pct_steady, True), money(b.vs_flat_2pct_steady, True)),
        ("Min spend OK", str(a.hits_min_spend), str(b.hits_min_spend)),
        ("Eligibility", a.eligibility, b.eligibility),
        ("Terms verified", verified_label(card_a, today), verified_label(card_b, today)),
    ]
    width = max(len(r[0]) for r in rows)
    col = max(12, *(len(r[1]) for r in rows))
    table = "\n".join(
        f"{label:<{width}}  {left:>{col}}  {right:>{col}}" for label, left, right in rows
    )
    header = f"{'':<{width}}  {'A':>{col}}  {'B':>{col}}"

    def winner(field: str, label: str) -> str:
        x, y = getattr(a, field), getattr(b, field)
        if abs(x - y) < 1:
            return f"{label}: a tie."
        best, gap = (a, x - y) if x > y else (b, y - x)
        return f"{label}: {best.name} by {money(gap)}."

    text = (
        f"A = {a.name}\nB = {b.name}\n```\n{header}\n{table}\n```\n"
        f"{winner('marginal_ev_year1', 'First year, given your wallet')}\n"
        f"{winner('marginal_ev_steady', 'Every year after')}"
    )
    notes = [f"{ev.name}: {note}" for ev in (a, b) for note in ev.notes]
    notes += [
        f"{MARKER} {ev.name}: {terms_warning(card, today)}."
        for ev, card in ((a, card_a), (b, card_b))
        if terms_warning(card, today)
    ]
    if notes:
        text += "\n" + "\n".join(notes)
    return {
        "display_text": text,
        "a": a.to_dict() | {"terms_warning": terms_warning(card_a, today)},
        "b": b.to_dict() | {"terms_warning": terms_warning(card_b, today)},
    }


def cmd_digest(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    warnings: list[str] = []
    if not args.no_sync:
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                sync(settings, client)
        except (httpx.HTTPError, SnapshotMissing, ValueError) as exc:
            warnings.append(
                f"Couldn't refresh card data ({type(exc).__name__}); using the cached copy."
            )
    warnings += _staleness(settings, now)
    ctx = build_context(settings, store, now)
    offers = store.personal_offers(since=now - timedelta(days=31))
    result = digest_mod.build_digest(ctx, load_changes(settings), offers, now, warnings)
    payload: dict[str, Any] = {
        "display_text": result.short,
        "email_markdown": result.full,
        "digest": result.to_dict(),
        "warnings": warnings,
    }
    if args.send_email:
        try:
            sent = mailer.send_digest(
                settings, f"Your card digest — {now:%B %Y}", result.full, result.period
            )
            store.log_digest(now, "email", result.period, sent)
            payload["email"] = {"sent": True, **sent}
        except (GuardrailError, inbox.InboxNotConfigured, ValueError) as exc:
            payload["email"] = {"sent": False, "error": str(exc)}
        except Exception as exc:  # network/API errors must not lose the WhatsApp text
            payload["email"] = {"sent": False, "error": f"{type(exc).__name__}: {exc}"}
    store.log_digest(now, "whatsapp", result.period, {"chars": len(result.short)})
    if args.print:
        payload["_print"] = result.short
    return payload


def cmd_inbox(args, settings: Settings, store: Store, now: datetime) -> dict[str, Any]:
    result = inbox.poll(settings, store, load_data(settings), now)
    lines = [
        f"Checked {result['checked']} message(s): {len(result['accepted'])} new offer(s), "
        f"{len(result['rejected'])} rejected, {result['already_seen']} already seen."
    ]
    for offer in result["accepted"]:
        if offer["kind"] == "forwarding_confirmation":
            continue
        flag = (
            " ⚠ suspected phishing, don't click anything in it"
            if offer["suspected_phishing"]
            else ""
        )
        amount = (
            f" {offer['bonus_amount']:,.0f} {offer['bonus_unit']}"
            if offer.get("bonus_amount")
            else ""
        )
        lines.append(f"• {offer['issuer']} {offer['kind'].replace('_', ' ')}{amount}{flag}")
    for item in result["action_required"]:
        lines.append(
            f"ACTION NEEDED: Gmail forwarding confirmation code {item['confirmation_code']}. "
            "Enter it in Gmail > Settings > Forwarding and POP/IMAP. I won't click the link for you."
        )
    return {"display_text": "\n".join(lines), **result}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class JsonErrorParser(argparse.ArgumentParser):
    """Usage errors become {"ok": false, ...} JSON instead of stderr text."""

    def error(self, message: str):
        raise CommandError(f"{message}\n{self.format_usage().strip()}")


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
    add.add_argument("card")
    add.add_argument("--opened", help="YYYY-MM-DD")
    add.add_argument("--fee-date", help="YYYY-MM-DD the annual fee posts")
    add.add_argument("--bonus-received", help="YYYY-MM-DD")
    add.add_argument("--product-changed-from", help="card you product-changed from")
    add.add_argument("--closed", help="YYYY-MM-DD (keeps history for issuer rules)")
    remove = wallet.add_parser("remove")
    remove.add_argument("card")
    wallet.add_parser("list")
    p.set_defaults(handler=cmd_wallet)

    p = sub.add_parser("rank", help="rank cards by marginal value to you")
    p.add_argument("--mode", choices=["travel", "cash_back", "business"])
    p.add_argument("--kind", choices=["personal", "business", "all"])
    p.add_argument("--max-af", type=float, help="max annual fee (default: your profile)")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--include-ineligible", action="store_true")
    p.add_argument("--json", action="store_true", help="include each card's itemized breakdown")
    p.set_defaults(handler=cmd_rank)

    p = sub.add_parser("compare", help="compare two cards side by side")
    p.add_argument("card_a")
    p.add_argument("card_b")
    p.set_defaults(handler=cmd_compare)

    p = sub.add_parser("explain", help="itemized math for one card")
    p.add_argument("card")
    p.set_defaults(handler=cmd_explain)

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


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    settings = Settings.from_env()
    try:
        args = build_parser().parse_args(argv)
    except CommandError as exc:
        emit({"ok": False, "command": None, "error": str(exc)}, settings)
        return 2
    now = now or datetime.now(UTC)
    try:
        store = Store(settings.db_path)
    except Exception as exc:  # e.g. unwritable CARD_AGENT_DB
        emit(
            {"ok": False, "command": args.command, "error": f"Can't open state DB: {exc}"}, settings
        )
        return 1
    try:
        payload = args.handler(args, settings, store, now)
        emit({"ok": True, "command": args.command, **payload}, settings)
        return 0
    except (
        CommandError,
        SnapshotMissing,
        GuardrailError,
        inbox.InboxNotConfigured,
        ValueError,
        httpx.HTTPError,
    ) as exc:
        emit({"ok": False, "command": args.command, "error": str(exc)}, settings)
        return 1
    finally:
        store.close()
