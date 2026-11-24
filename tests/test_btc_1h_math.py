"""Fair-value maths in the BTC 1h bot: normal CDF and the GBM probability."""
import pytest

from btc_1h_bot import _calc_fair_yes, _norm_cdf, _parse_prices


def test_norm_cdf_known_values():
    assert _norm_cdf(0) == pytest.approx(0.5)
    assert _norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
    assert _norm_cdf(-1.0) == pytest.approx(1 - _norm_cdf(1.0))


def test_fair_value_at_strike_is_a_coin_flip():
    assert _calc_fair_yes(60_000, 60_000, 1800, 0.5) == pytest.approx(0.5)


def test_fair_value_rises_as_time_runs_out():
    early = _calc_fair_yes(60_060, 60_000, 3000, 0.5)
    late = _calc_fair_yes(60_060, 60_000, 300, 0.5)
    assert 0.5 < early < late


def test_fair_value_is_symmetric():
    up = _calc_fair_yes(60_060, 60_000, 1200, 0.5)
    down = _calc_fair_yes(59_940, 60_000, 1200, 0.5)
    assert up + down == pytest.approx(1.0, abs=1e-3)


def test_fair_value_is_clamped_while_window_open():
    assert _calc_fair_yes(70_000, 60_000, 1800, 0.5) == 0.95
    assert _calc_fair_yes(50_000, 60_000, 1800, 0.5) == 0.05


def test_fair_value_after_close_is_decided():
    assert _calc_fair_yes(60_100, 60_000, 0, 0.5) == 1.0
    assert _calc_fair_yes(59_900, 60_000, 0, 0.5) == 0.0


def test_exact_tie_at_close_counts_as_up():
    # Polymarket's 1h rules settle close == open as UP.
    assert _calc_fair_yes(60_000, 60_000, 0, 0.5) == 1.0
    # Same rule when remaining volatility rounds to zero just before the close.
    assert _calc_fair_yes(60_000, 60_000, 0.001, 0.5) == 1.0


def test_parse_prices():
    assert _parse_prices('["0.42", "0.58"]') == [0.42, 0.58]
    assert _parse_prices([0.1, "0.9"]) == [0.1, 0.9]
    assert _parse_prices("not json") == []
