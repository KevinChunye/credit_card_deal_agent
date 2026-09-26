from datetime import date

from card_agent.collector import bonuses_api
from card_agent.eligibility import EligibilityChecker, load_rules
from card_agent.models import WalletCard
from tests.conftest import NOW

TODAY = date(2026, 9, 26)


def cards(api_raw):
    normalized, *_ = bonuses_api.normalize(api_raw, NOW)
    return {card.id: card for card in normalized}


def check(api_raw, card_id, wallet):
    all_cards = cards(api_raw)
    checker = EligibilityChecker(load_rules(), wallet, all_cards, TODAY)
    return checker.check(all_cards[card_id])


def personal_cards_opened(n, start_year=2025):
    ids = ["amex-gold", "citi-double-cash", "wells-fargo-active-cash", "capital-one-savor", "bofa-premium-rewards", "amex-green"]  # fmt: skip
    return [
        WalletCard(card_id=cid, opened_on=date(start_year, 1 + i, 1))
        for i, cid in enumerate(ids[:n])
    ]


def test_chase_5_24(api_raw):
    assert check(api_raw, "chase-freedom-flex", personal_cards_opened(4)).status == "eligible"
    result = check(api_raw, "chase-freedom-flex", personal_cards_opened(5))
    assert result.status == "ineligible"
    assert "chase-5-24" in result.rule_ids
    # Cards opened more than 24 months ago don't count.
    assert (
        check(api_raw, "chase-freedom-flex", personal_cards_opened(5, start_year=2023)).status
        == "eligible"
    )


def test_5_24_business_cards_mostly_dont_count(api_raw):
    wallet = personal_cards_opened(4) + [
        WalletCard(card_id="amex-business-platinum", opened_on=date(2026, 1, 1))
    ]
    assert check(api_raw, "chase-sapphire-reserve", wallet).status == "eligible"


def test_5_24_undated_cards_make_it_unknown(api_raw):
    wallet = personal_cards_opened(3) + [
        WalletCard(card_id="amex-platinum"),
        WalletCard(card_id="citi-custom-cash"),
    ]
    assert check(api_raw, "chase-freedom-flex", wallet).status == "unknown"


def test_5_24_product_change_is_not_a_new_account(api_raw):
    wallet = personal_cards_opened(4) + [
        WalletCard(card_id="chase-freedom-unlimited", opened_on=date(2026, 3, 1), product_changed_from="chase-sapphire-preferred")
    ]  # fmt: skip
    assert check(api_raw, "chase-freedom-flex", wallet).status == "eligible"


def test_sapphire_family(api_raw):
    holding_csp = [WalletCard(card_id="chase-sapphire-preferred", opened_on=date(2021, 1, 1))]
    assert check(api_raw, "chase-sapphire-preferred", holding_csp).status == "ineligible"
    reserve = check(api_raw, "chase-sapphire-reserve", holding_csp)
    assert reserve.status == "unknown"  # legacy one-Sapphire rule: check the pop-up
    # Product-changed out of CSP still counts as "have had".
    pc = [
        WalletCard(
            card_id="chase-freedom-unlimited", product_changed_from="chase-sapphire-preferred"
        )
    ]
    assert check(api_raw, "chase-sapphire-preferred", pc).status == "ineligible"


def test_amex_lifetime_and_family(api_raw):
    had_gold = [WalletCard(card_id="amex-gold", closed_on=date(2024, 1, 1))]
    assert check(api_raw, "amex-gold", had_gold).status == "ineligible"
    assert check(api_raw, "amex-green", had_gold).status == "ineligible"
    assert check(api_raw, "amex-platinum", had_gold).status == "eligible"

    had_platinum = [WalletCard(card_id="amex-platinum")]
    gold = check(api_raw, "amex-gold", had_platinum)
    assert gold.status == "ineligible" and "amex-gold-family" in gold.rule_ids

    had_bcp = [WalletCard(card_id="amex-blue-cash-preferred")]
    assert check(api_raw, "amex-blue-cash-everyday", had_bcp).status == "ineligible"
    assert (
        check(
            api_raw, "amex-blue-cash-preferred", [WalletCard(card_id="amex-blue-cash-everyday")]
        ).status
        == "eligible"
    )

    had_delta_plat = [WalletCard(card_id="amex-delta-skymiles-platinum")]
    assert check(api_raw, "amex-delta-skymiles-gold", had_delta_plat).status == "ineligible"
    assert check(api_raw, "amex-delta-skymiles-blue", had_delta_plat).status == "ineligible"


def test_citi_48_months(api_raw):
    recent = [
        WalletCard(
            card_id="citi-strata-premier",
            bonus_received_on=date(2024, 1, 15),
            closed_on=date(2025, 6, 1),
        )
    ]
    result = check(api_raw, "citi-strata-premier", recent)
    assert result.status == "ineligible" and "citi-48-month" in result.rule_ids
    old = [
        WalletCard(
            card_id="citi-strata-premier",
            bonus_received_on=date(2021, 1, 15),
            closed_on=date(2022, 6, 1),
        )
    ]
    assert check(api_raw, "citi-strata-premier", old).status == "eligible"
    # A bonus on a different Citi card doesn't block this one.
    other = [WalletCard(card_id="citi-custom-cash", bonus_received_on=date(2025, 1, 15))]
    assert check(api_raw, "citi-strata-premier", other).status == "eligible"


def test_capital_one_venture_family(api_raw):
    venture_bonus = [
        WalletCard(card_id="capital-one-venture-rewards", bonus_received_on=date(2025, 5, 1))
    ]
    result = check(api_raw, "capital-one-venture-x", venture_bonus)
    assert result.status == "ineligible"
    assert "capital-one-venture-family" in result.rule_ids
    savor = [WalletCard(card_id="capital-one-savor", bonus_received_on=date(2025, 5, 1))]
    assert check(api_raw, "capital-one-venture-x", savor).status == "eligible"
    previous_x = [WalletCard(card_id="capital-one-venture-x", closed_on=date(2020, 1, 1))]
    assert check(api_raw, "capital-one-venture-x", previous_x).status == "unknown"
