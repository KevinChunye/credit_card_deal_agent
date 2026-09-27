"""Chat-ready text: plain words, emoji and Unicode bar charts.

Everything the agent relays lands in a chat app (Maritime, WhatsApp) or an
email, so display text never contains markup a person would read as syntax:
no Markdown headings, asterisks, underscores, backticks, code fences or
[text](url) links. URLs are written out in full; chat apps make them
clickable. `markup_leaks` is the check the tests and the evaluation run on
every display text.
"""

from __future__ import annotations

import re

from card_agent.models import Category

BAR_WIDTH = 10
FULL, EMPTY = "█", "░"
KEYCAPS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

CATEGORY: dict[Category, tuple[str, str]] = {
    Category.dining: ("🍽️", "Dining"),
    Category.groceries: ("🛒", "Groceries"),
    Category.online_groceries: ("🛍️", "Online groceries"),
    Category.gas: ("⛽", "Gas"),
    Category.ev_charging: ("🔌", "EV charging"),
    Category.travel_portal: ("🧳", "Travel booked through the card's portal"),
    Category.travel_general: ("🌍", "Other travel"),
    Category.flights: ("✈️", "Flights"),
    Category.hotels: ("🏨", "Hotels"),
    Category.transit_rideshare: ("🚕", "Transit and rideshare"),
    Category.streaming: ("📺", "Streaming"),
    Category.drugstores: ("💊", "Drugstores"),
    Category.rent: ("🏠", "Rent"),
    Category.mobile_wallet: ("📱", "Mobile wallet"),
    Category.rotating: ("🔄", "Rotating 5% categories"),
    Category.other: ("🧾", "Everything else"),
}

CURRENCY: dict[str, str] = {
    "usd": "cash back",
    "chase_ur": "Chase Ultimate Rewards",
    "amex_mr": "Amex Membership Rewards",
    "capital_one_miles": "Capital One miles",
    "citi_typ": "Citi ThankYou points",
    "bilt": "Bilt points",
    "wells_fargo_rewards": "Wells Fargo Rewards",
    "us_bank_points": "U.S. Bank points",
    "bofa_points": "Bank of America points",
    "barclays_points": "Barclays points",
    "hyatt": "World of Hyatt points",
    "marriott": "Marriott Bonvoy points",
    "hilton": "Hilton Honors points",
    "ihg": "IHG One Rewards points",
    "wyndham": "Wyndham Rewards points",
    "choice": "Choice Privileges points",
    "best_western": "Best Western Rewards points",
    "radisson": "Radisson Rewards points",
    "delta": "Delta SkyMiles",
    "united": "United MileagePlus miles",
    "american": "American AAdvantage miles",
    "southwest": "Southwest Rapid Rewards points",
    "alaska": "Alaska Atmos points",
    "jetblue": "JetBlue TrueBlue points",
    "avios": "Avios",
    "aeroplan": "Aeroplan points",
    "flying_blue": "Flying Blue miles",
}


def category_emoji(category: Category | str) -> str:
    return CATEGORY[Category(category)][0]


def category_label(category: Category | str) -> str:
    return CATEGORY[Category(category)][1]


def currency_label(code: str) -> str:
    return CURRENCY.get(code, code.replace("_", " ").title())


def number(index: int) -> str:
    """1️⃣ … 🔟, then plain "11." for longer lists."""
    return KEYCAPS[index - 1] if 1 <= index <= len(KEYCAPS) else f"{index}."


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else (many or word + 's')}"


def bar(value: float, top: float, width: int = BAR_WIDTH) -> str:
    """A `width`-cell bar filled in proportion to value/top; at least one cell
    when the value is positive, none when it's zero or negative."""
    if top <= 0 or value <= 0:
        return EMPTY * width
    filled = max(1, round(width * min(value, top) / top))
    return FULL * filled + EMPTY * (width - filled)


def bar_lines(rows: list[tuple[float, str]], width: int = BAR_WIDTH) -> list[str]:
    """One line per (value, caption): the bar first, so bars line up in any font."""
    top = max((value for value, _ in rows), default=0.0)
    return [f"{bar(value, top, width)} {caption}" for value, caption in rows]


_LEAKS: list[tuple[str, re.Pattern[str]]] = [
    ("code fence", re.compile(r"```")),
    ("backtick", re.compile(r"`")),
    ("bold markup", re.compile(r"\*\*|__")),
    ("heading markup", re.compile(r"^\s{0,3}#{1,6}\s", re.M)),
    ("link markup", re.compile(r"\[[^\]\n]+\]\([^)\s]+\)")),
    ("asterisk emphasis", re.compile(r"(?<![\w*])\*(?=\S)[^*\n]+?(?<=\S)\*(?![\w*])")),
    ("underscore emphasis", re.compile(r"(?<![\w_])_(?=\S)[^_\n]+?(?<=\S)_(?![\w_])")),
    ("html tag", re.compile(r"</?[a-zA-Z][^>\n]*>")),
    ("json", re.compile(r"[{\[]\s*\"")),
]


def markup_leaks(text: str) -> list[str]:
    """Names of the markup kinds found in `text` (empty when it reads clean)."""
    return [name for name, pattern in _LEAKS if pattern.search(text or "")]


# Words people use for each spend category ("which card for Uber?").
CATEGORY_WORDS: dict[Category, tuple[str, ...]] = {
    Category.dining: ("dining", "restaurant", "restaurants", "food", "eating out", "takeout", "delivery"),
    Category.groceries: ("grocery", "groceries", "supermarket", "supermarkets"),
    Category.online_groceries: ("online grocery", "online groceries", "instacart", "grocery delivery"),
    Category.gas: ("gas", "fuel", "gas station", "gas stations", "petrol"),
    Category.ev_charging: ("ev", "ev charging", "charging", "electric vehicle charging"),
    Category.travel_portal: ("portal", "travel portal", "card portal"),
    Category.travel_general: ("travel", "trip", "trips", "vacation"),
    Category.flights: ("flight", "flights", "airfare", "airline", "airlines", "plane tickets"),
    Category.hotels: ("hotel", "hotels", "lodging"),
    Category.transit_rideshare: ("transit", "uber", "lyft", "rideshare", "taxi", "subway", "train", "bus"),
    Category.streaming: ("streaming", "netflix", "spotify", "hulu", "disney plus"),
    Category.drugstores: ("drugstore", "drugstores", "pharmacy", "cvs", "walgreens"),
    Category.rent: ("rent",),
    Category.mobile_wallet: ("mobile wallet", "apple pay", "google pay"),
    Category.rotating: ("rotating", "rotating categories", "quarterly categories"),
    Category.other: ("other", "everything", "everything else", "anything", "shopping", "online shopping"),
}  # fmt: skip


def parse_category(text: str) -> Category | None:
    """A spend category from everyday words, or None if unclear."""
    wanted = re.sub(r"[^a-z0-9% ]+", " ", text.lower()).strip()
    wanted = re.sub(r"\s+", " ", wanted)
    for category in Category:
        if wanted in (
            category.value,
            category.value.replace("_", " "),
            category_label(category).lower(),
        ):
            return category
    for category, words in CATEGORY_WORDS.items():
        if wanted in words:
            return category
    return None
