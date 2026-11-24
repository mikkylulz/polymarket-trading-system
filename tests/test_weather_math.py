"""Pure maths behind the weather bot: bucket parsing, bucket probability, EV and Kelly."""
import math

import pytest

from config import KELLY_FRACTION, LOCATIONS, MAX_BET, MIN_BET
from signals import (_bucket_celsius, _f_to_c, bucket_probability, calc_ev,
                     calc_kelly, get_sigma, parse_bucket)


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@pytest.mark.parametrize("label, expected", [
    ("15°C", 15),
    ("62°F or higher", 62),
    ("-3°C or lower", -3),
    ("31", 31),
    ("no number here", None),
])
def test_parse_bucket(label, expected):
    assert parse_bucket(label) == expected


def test_fahrenheit_conversion():
    assert _f_to_c(32) == 0
    assert _f_to_c(212) == pytest.approx(100)
    assert _bucket_celsius("50°F", 50) == pytest.approx(10)
    assert _bucket_celsius("15°C", 15) == 15.0


def test_bucket_probability_centred_forecast():
    # Forecast exactly on the bucket: P = Phi(0.5/sigma) - Phi(-0.5/sigma)
    assert bucket_probability(20, 20.0, 1.0) == pytest.approx(_phi(0.5) - _phi(-0.5))


def test_bucket_probability_falls_with_distance():
    near = bucket_probability(20, 20.0, 1.5)
    off = bucket_probability(21, 20.0, 1.5)
    far = bucket_probability(23, 20.0, 1.5)
    assert near > off > far


def test_bucket_probability_is_clamped():
    assert bucket_probability(40, 20.0, 1.0) == 0.01   # hopeless bucket floors at 1%
    assert bucket_probability(20, 20.0, 0.01) == 0.99  # near-certain caps at 99%


def test_bucket_probability_without_forecast_uses_prior():
    assert bucket_probability(20, None, 1.0) == 0.20
    assert bucket_probability(20, 20.0, 0) == 0.20


@pytest.mark.parametrize("p, price, expected", [
    (0.5, 0.5, 0.0),     # fair price: zero edge
    (0.6, 0.5, 0.2),     # 60% at 50c: +0.20 per $1 staked
    (0.1, 0.2, -0.5),    # overpaying
])
def test_calc_ev(p, price, expected):
    assert calc_ev(p, price) == pytest.approx(expected)


@pytest.mark.parametrize("price", [0, 1, -0.1, 1.5])
def test_calc_ev_rejects_impossible_prices(price):
    assert calc_ev(0.5, price) == 0.0


def test_calc_kelly_no_edge_returns_minimum_bet():
    assert calc_kelly(0.3, 0.5, 1000) == MIN_BET


def test_calc_kelly_matches_fractional_kelly_between_limits():
    p, price = 0.55, 0.5
    b = 1 / price - 1
    raw = (p * b - (1 - p)) / b * KELLY_FRACTION
    balance = (MIN_BET + MAX_BET) / 2 / raw   # chosen so the bet lands inside [MIN, MAX]
    assert calc_kelly(p, price, balance) == pytest.approx(raw * balance, abs=0.01)


def test_calc_kelly_is_capped():
    assert calc_kelly(0.95, 0.2, 1_000_000) == MAX_BET


def test_calc_kelly_impossible_price_returns_minimum_bet():
    assert calc_kelly(0.9, 0, 1000) == MIN_BET


def test_get_sigma_prefers_learned_calibration():
    city = next(iter(LOCATIONS))
    default = LOCATIONS[city].get("sigma", 2.0)
    assert get_sigma(city, {}) == default
    assert get_sigma(city, {f"{city}_ecmwf": {"sigma": 1.7}}) == 1.7
    assert get_sigma(city, {f"{city}_ecmwf": {"sigma": 1.7},
                            f"{city}_best": {"sigma": 1.1}}) == 1.1
