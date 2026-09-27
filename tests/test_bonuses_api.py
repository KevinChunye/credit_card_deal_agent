from card_agent.collector import bonuses_api
from card_agent.models import BenefitKind, BonusUnit, Cadence, Category
from tests.conftest import NOW


def normalized(api_raw):
    cards, offers, benefits, base_rates = bonuses_api.normalize(api_raw, NOW)
    return (
        {c.id: c for c in cards},
        offers,
        benefits,
        base_rates,
    )


def test_ids_are_readable_and_unique(api_raw):
    cards, *_ = normalized(api_raw)
    assert "chase-sapphire-preferred" in cards
    assert "amex-gold" in cards
    assert "capital-one-venture-x" in cards
    assert len(cards) == len(api_raw)


def test_duplicate_names_get_stable_suffix(api_raw):
    cards, *_ = normalized(api_raw)
    expedia = sorted(cid for cid in cards if cid.startswith("wells-fargo-expedia-one-key"))
    assert len(expedia) == 2
    # The lower annual fee keeps the plain id.
    assert cards["wells-fargo-expedia-one-key"].annual_fee == 0
    other = [cid for cid in expedia if cid != "wells-fargo-expedia-one-key"][0]
    assert cards[other].annual_fee == 99


def test_card_fields(api_raw):
    cards, *_ = normalized(api_raw)
    csp = cards["chase-sapphire-preferred"]
    assert csp.issuer == "chase"
    assert csp.point_currency == "chase_ur"
    assert csp.transferable is True
    assert csp.annual_fee == 95
    assert csp.is_business is False
    assert cards["amex-blue-cash-preferred"].point_currency == "usd"
    assert cards["amex-everyday"].discontinued is True


def test_offers(api_raw):
    _, offers, _, _ = normalized(api_raw)
    csp = [o for o in offers if o.card_id == "chase-sapphire-preferred"]
    assert len(csp) == 1
    offer = csp[0]
    assert offer.bonus_amount == 75000
    assert offer.bonus_unit == BonusUnit.points
    assert offer.min_spend == 5000
    assert offer.spend_window_days == 90
    assert offer.historical_high == 100000
    assert offer.is_elevated is False  # below the 100k it has been
    venture = [o for o in offers if o.card_id == "capital-one-venture-x"][0]
    assert venture.bonus_unit == BonusUnit.miles
    # Zero-amount offers are dropped (discontinued Amex EveryDay has one).
    assert not [o for o in offers if o.card_id == "amex-everyday"]


def test_credits_become_classified_benefits(api_raw):
    _, _, benefits, _ = normalized(api_raw)
    gold = {b.name: b for b in benefits if b.card_id == "amex-gold"}
    assert gold["$10/mo credit for Uber"].kind == BenefitKind.rideshare_credit
    assert gold["$10/mo credit for Uber"].cadence == Cadence.monthly
    assert gold["Semi-annual $50 Resy credit"].kind == BenefitKind.dining_credit
    assert gold["Semi-annual $50 Resy credit"].cadence == Cadence.semiannual
    platinum = {b.name: b for b in benefits if b.card_id == "amex-platinum"}
    assert platinum["Uber One Membership"].kind == BenefitKind.dining_membership
    assert platinum["Lounge Access"].kind == BenefitKind.lounge
    precheck = platinum["PreCheck Credit"]
    assert precheck.kind == BenefitKind.global_entry
    assert precheck.face_value_annual == 25  # $100 every ~4 years, annualized


def test_base_rate_fallback(api_raw):
    _, _, _, base_rates = normalized(api_raw)
    venture = [r for r in base_rates if r.card_id == "capital-one-venture-x"]
    assert venture[0].category == Category.other
    assert venture[0].multiplier == 2


def test_classify_credit_order():
    assert bonuses_api.classify_credit("Uber One Membership") == BenefitKind.dining_membership
    assert bonuses_api.classify_credit("$15/mo Uber Cash") == BenefitKind.rideshare_credit
    assert bonuses_api.classify_credit("Misc. Hotel Perks") == BenefitKind.other
    assert bonuses_api.classify_credit("Travel Credit") == BenefitKind.travel_credit
    assert bonuses_api.classify_credit("Equinox Credit") == BenefitKind.other


def test_points_denominated_credits_and_offer_extras():
    """Real API quirks: credit values can be in points, and offers can carry a
    free-night certificate in the card's own currency."""
    raw = [
        {
            "cardId": "abc123",
            "name": "Marriott Bonvoy Bold",
            "issuer": "CHASE",
            "network": "VISA",
            "currency": "MARRIOTT",
            "isBusiness": False,
            "annualFee": 0,
            "isAnnualFeeWaived": False,
            "universalCashbackPercent": 1,
            "url": "https://example.com",
            "imageUrl": "/x.png",
            "credits": [
                {
                    "description": "Anniversary Points",
                    "value": 7500,
                    "weight": 1,
                    "currency": "MARRIOTT",
                },
                {
                    "description": "$60 Hilton credit per quarter.",
                    "value": 240,
                    "weight": 0.5,
                    "currency": "HILTON",
                },
                {
                    "description": "10k award flight discount",
                    "value": 10000,
                    "weight": 0.75,
                    "currency": "UNITED",
                },
            ],
            "offers": [
                {
                    "spend": 1000,
                    "amount": [{"amount": 60000}],
                    "days": 90,
                    "credits": [
                        {
                            "description": "1x FNC, <= 50k",
                            "value": 50000,
                            "weight": 0.7,
                            "currency": "MARRIOTT",
                        },
                        {"description": "Statement Credit", "value": 100, "weight": 1},
                    ],
                }
            ],
            "historicalOffers": [],
            "discontinued": False,
        }
    ]
    _, offers, benefits, _ = bonuses_api.normalize(raw, NOW)
    offer = offers[0]
    assert (offer.bonus_amount, offer.extra_points, offer.extra_usd) == (60000, 50000, 100)
    by_name = {b.name: b for b in benefits}
    anniversary = by_name["Anniversary Points"]
    assert (anniversary.value_currency, anniversary.automatic) == ("marriott", True)
    assert by_name["$60 Hilton credit per quarter."].value_currency == "usd"  # "$" wins
    assert by_name["10k award flight discount"].value_currency == "united"
    assert by_name["10k award flight discount"].kind == BenefitKind.other
