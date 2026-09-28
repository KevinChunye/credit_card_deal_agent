"""Private state in local SQLite (path from CARD_AGENT_DB). Never committed.

Holds only what you tell it (profile, spend, valuations, haircuts, wallet,
cards or issuers to hide), offers parsed from your own forwarded email, and
the agent's own history (digests sent, past recommendations). No card
numbers, no bank credentials: `guardrails.reject_card_numbers` runs on every
free-text field.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from card_agent.guardrails import reject_card_numbers, sanitize_untrusted
from card_agent.models import (
    DEFAULT_HAIRCUTS,
    DEFAULT_VALUATIONS,
    BenefitKind,
    Category,
    PersonalOffer,
    UserProfile,
    WalletCard,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monthly_spend (
    category TEXT PRIMARY KEY,
    amount REAL NOT NULL CHECK (amount >= 0)
);
CREATE TABLE IF NOT EXISTS point_valuation (
    currency TEXT PRIMARY KEY,
    cpp REAL NOT NULL CHECK (cpp > 0)
);
CREATE TABLE IF NOT EXISTS usage_haircut (
    kind TEXT PRIMARY KEY,
    factor REAL NOT NULL CHECK (factor >= 0 AND factor <= 1)
);
CREATE TABLE IF NOT EXISTS wallet_card (
    card_id TEXT PRIMARY KEY,
    opened_on TEXT,
    annual_fee_date TEXT,
    bonus_received_on TEXT,
    product_changed_from TEXT,
    closed_on TEXT
);
CREATE TABLE IF NOT EXISTS personal_offer (
    message_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_message (
    message_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    period TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS hidden (
    kind TEXT NOT NULL CHECK (kind IN ('card', 'issuer')),
    value TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
CREATE TABLE IF NOT EXISTS recommendation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    goal TEXT NOT NULL,
    card_id TEXT,
    verdict TEXT,
    detail TEXT
);
"""


# Bumped when stored data needs a one-time fix-up; see Store._migrate.
SCHEMA_VERSION = 1
# A scheduled digest and a chat command can run at the same time: wait this
# long for the other writer instead of failing with "database is locked".
LOCK_TIMEOUT_SECONDS = 30


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def utc_iso(value: datetime) -> str:
    """One timezone for every stored timestamp, so text comparisons in SQL are
    also time comparisons. Naive datetimes are taken as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=LOCK_TIMEOUT_SECONDS)
        self.conn.row_factory = sqlite3.Row
        self._depth = 0
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[None]:
        """Group writes: all of them are saved, or none (rolled back on error).
        immediate takes the write lock up front, so a read-modify-write inside
        can't lose a concurrent update."""
        if immediate and not self._depth:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            if self._depth == 1:
                self.conn.rollback()
            raise
        else:
            if self._depth == 1:
                self.conn.commit()
        finally:
            self._depth -= 1

    def _commit(self) -> None:
        if not self._depth:
            self.conn.commit()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            # v1: personal offers' received_at in UTC (older rows kept the
            # sender's offset, which broke the "last 31 days" filter).
            rows = self.conn.execute("SELECT message_id, received_at FROM personal_offer")
            for row in rows.fetchall():
                try:
                    fixed = utc_iso(datetime.fromisoformat(row["received_at"]))
                except (TypeError, ValueError):
                    continue  # unreadable: leave it rather than refuse to open the DB
                self.conn.execute(
                    "UPDATE personal_offer SET received_at = ? WHERE message_id = ?",
                    (fixed, row["message_id"]),
                )
        if version < SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ------------------------------------------------------------- profile
    def get_profile(self) -> UserProfile:
        row = self.conn.execute("SELECT data FROM user_profile WHERE id = 1").fetchone()
        return UserProfile.model_validate_json(row["data"]) if row else UserProfile()

    def has_profile(self) -> bool:
        return self.conn.execute("SELECT 1 FROM user_profile WHERE id = 1").fetchone() is not None

    def save_profile(self, profile: UserProfile) -> None:
        body = profile.model_dump_json()
        reject_card_numbers(body)
        self.conn.execute(
            "INSERT INTO user_profile (id, data) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (body,),
        )
        self._commit()

    # --------------------------------------------------------------- spend
    def get_spend(self) -> dict[Category, float]:
        rows = self.conn.execute("SELECT category, amount FROM monthly_spend").fetchall()
        return {Category(row["category"]): row["amount"] for row in rows}

    def set_spend(self, spend: dict[Category, float], replace: bool = False) -> None:
        if any(float(amount) < 0 for amount in spend.values()):
            raise ValueError("Monthly spend can't be negative.")
        if replace:
            self.conn.execute("DELETE FROM monthly_spend")
        self.conn.executemany(
            "INSERT INTO monthly_spend (category, amount) VALUES (?, ?) "
            "ON CONFLICT(category) DO UPDATE SET amount = excluded.amount",
            [(Category(category).value, float(amount)) for category, amount in spend.items()],
        )
        self._commit()

    # ---------------------------------------------------------- valuations
    def get_valuations(self) -> dict[str, float]:
        """Defaults overlaid with whatever you've set."""
        rows = self.conn.execute("SELECT currency, cpp FROM point_valuation").fetchall()
        return {**DEFAULT_VALUATIONS, **{row["currency"]: row["cpp"] for row in rows}}

    def custom_valuations(self) -> dict[str, float]:
        """Only the valuations you set yourself (no defaults)."""
        rows = self.conn.execute(
            "SELECT currency, cpp FROM point_valuation ORDER BY currency"
        ).fetchall()
        return {row["currency"]: row["cpp"] for row in rows}

    def set_valuations(self, valuations: dict[str, float]) -> None:
        self.conn.executemany(
            "INSERT INTO point_valuation (currency, cpp) VALUES (?, ?) "
            "ON CONFLICT(currency) DO UPDATE SET cpp = excluded.cpp",
            [(currency, float(cpp)) for currency, cpp in valuations.items()],
        )
        self._commit()

    # ------------------------------------------------------------ haircuts
    def get_haircuts(self) -> dict[BenefitKind, float]:
        rows = self.conn.execute("SELECT kind, factor FROM usage_haircut").fetchall()
        return {**DEFAULT_HAIRCUTS, **{BenefitKind(row["kind"]): row["factor"] for row in rows}}

    def set_haircuts(self, haircuts: dict[BenefitKind, float]) -> None:
        self.conn.executemany(
            "INSERT INTO usage_haircut (kind, factor) VALUES (?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET factor = excluded.factor",
            [(BenefitKind(kind).value, float(factor)) for kind, factor in haircuts.items()],
        )
        self._commit()

    # -------------------------------------------------------------- wallet
    def list_wallet(self, include_closed: bool = True) -> list[WalletCard]:
        rows = self.conn.execute("SELECT * FROM wallet_card ORDER BY card_id").fetchall()
        cards = [WalletCard.model_validate(dict(row)) for row in rows]
        return cards if include_closed else [card for card in cards if card.is_open]

    def get_wallet_card(self, card_id: str) -> WalletCard | None:
        row = self.conn.execute("SELECT * FROM wallet_card WHERE card_id = ?", (card_id,))
        found = row.fetchone()
        return WalletCard.model_validate(dict(found)) if found else None

    def upsert_wallet_card(self, card: WalletCard) -> WalletCard:
        """Add a card, or update one you hold. Only the dates given change: an
        empty field keeps what's stored (setting a fee date must not erase the
        open date that issuer rules like 5/24 depend on). Returns the saved card."""
        existing = self.get_wallet_card(card.card_id)
        if existing:
            given = {k: v for k, v in card.model_dump().items() if v is not None}
            card = existing.model_copy(update=given)
        if card.opened_on and card.closed_on and card.closed_on < card.opened_on:
            raise ValueError(
                f"The close date ({card.closed_on}) is before the open date ({card.opened_on})."
            )
        reject_card_numbers(card.model_dump_json())
        self.conn.execute(
            "INSERT INTO wallet_card (card_id, opened_on, annual_fee_date, bonus_received_on, "
            "product_changed_from, closed_on) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(card_id) DO UPDATE SET opened_on = excluded.opened_on, "
            "annual_fee_date = excluded.annual_fee_date, "
            "bonus_received_on = excluded.bonus_received_on, "
            "product_changed_from = excluded.product_changed_from, "
            "closed_on = excluded.closed_on",
            (
                card.card_id,
                _iso(card.opened_on),
                _iso(card.annual_fee_date),
                _iso(card.bonus_received_on),
                card.product_changed_from,
                _iso(card.closed_on),
            ),
        )
        self._commit()
        return card

    def remove_wallet_card(self, card_id: str) -> bool:
        cursor = self.conn.execute("DELETE FROM wallet_card WHERE card_id = ?", (card_id,))
        self._commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------ personal offers
    def is_processed(self, message_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed_message WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark_processed(self, message_id: str, outcome: str, when: datetime) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO processed_message (message_id, processed_at, outcome) "
            "VALUES (?, ?, ?)",
            (message_id, when.isoformat(), outcome),
        )
        self._commit()

    def save_personal_offer(self, offer: PersonalOffer) -> None:
        body = offer.model_dump_json()
        reject_card_numbers(body)
        self.conn.execute(
            "INSERT OR REPLACE INTO personal_offer (message_id, received_at, data) VALUES (?, ?, ?)",
            (offer.message_id, utc_iso(offer.received_at), body),
        )
        self._commit()

    def personal_offers(self, since: datetime | None = None) -> list[PersonalOffer]:
        if since is None:
            rows = self.conn.execute(
                "SELECT data FROM personal_offer ORDER BY received_at DESC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT data FROM personal_offer WHERE received_at >= ? ORDER BY received_at DESC",
                (utc_iso(since),),
            ).fetchall()
        return [PersonalOffer.model_validate_json(row["data"]) for row in rows]

    # --------------------------------------------------------------- digest
    def log_digest(self, when: datetime, channel: str, period: str, detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO digest_log (sent_at, channel, period, detail) VALUES (?, ?, ?, ?)",
            (when.isoformat(), channel, period, json.dumps(detail)),
        )
        self._commit()

    def last_digest(self, channel: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM digest_log WHERE channel = ? ORDER BY id DESC LIMIT 1", (channel,)
        ).fetchone()
        return dict(row) if row else None

    # --------------------------------------------------------------- hidden
    def hide(self, kind: str, value: str, reason: str | None, when: datetime) -> None:
        if reason:
            reject_card_numbers(reason)  # refuse, before scrubbing could hide it
            reason = sanitize_untrusted(reason, 160)
        self.conn.execute(
            "INSERT INTO hidden (kind, value, reason, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(kind, value) DO UPDATE SET reason = COALESCE(excluded.reason, hidden.reason)",
            (kind, value, reason, when.isoformat()),
        )
        self._commit()

    def unhide(self, kind: str, value: str) -> bool:
        cursor = self.conn.execute("DELETE FROM hidden WHERE kind = ? AND value = ?", (kind, value))
        self._commit()
        return cursor.rowcount > 0

    def hidden(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM hidden ORDER BY created_at, value").fetchall()
        return [dict(row) for row in rows]

    def hidden_values(self, kind: str) -> set[str]:
        rows = self.conn.execute("SELECT value FROM hidden WHERE kind = ?", (kind,)).fetchall()
        return {row["value"] for row in rows}

    # -------------------------------------------------------- recommendations
    def log_recommendation(
        self, when: datetime, goal: str, card_id: str | None, verdict: str | None, detail: dict
    ) -> None:
        self.conn.execute(
            "INSERT INTO recommendation (created_at, goal, card_id, verdict, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (when.isoformat(), goal, card_id, verdict, json.dumps(detail)),
        )
        self._commit()

    def recommendations(self, limit: int = 5, goal: str | None = None) -> list[dict]:
        if goal is None:
            rows = self.conn.execute(
                "SELECT * FROM recommendation ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM recommendation WHERE goal = ? ORDER BY id DESC LIMIT ?",
                (goal, limit),
            ).fetchall()
        return [dict(row) | {"detail": json.loads(row["detail"] or "{}")} for row in rows]

    def counts(self) -> dict[str, int]:
        """Row counts for the memory view."""
        tables = ("personal_offer", "processed_message", "digest_log", "recommendation")
        return {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
