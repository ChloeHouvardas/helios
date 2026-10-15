"""D6 tests: conformal calibration utility."""
import numpy as np
import pytest

from helios_forecast import ForecastError, conformal_half_width, empirical_coverage


# --- exact quantile math ---------------------------------------------------


def test_half_width_is_finite_sample_corrected_quantile():
    errors = list(range(1, 11))  # 1..10
    # alpha=0.1: k = ceil(11 * 0.9) = 10 → 10th smallest = 10
    assert conformal_half_width(errors, alpha=0.1) == pytest.approx(10.0)
    # alpha=0.5: k = ceil(11 * 0.5) = 6 → 6th smallest = 6
    assert conformal_half_width(errors, alpha=0.5) == pytest.approx(6.0)
    # alpha=0.9: k = ceil(11 * 0.1) = 2 → 2nd smallest = 2
    assert conformal_half_width(errors, alpha=0.9) == pytest.approx(2.0)


def test_half_width_handles_unsorted_input():
    errors = [3.0, 1.0, 4.0, 1.0, 5.0]
    # k = ceil(6 * 0.5) = 3 → 3rd smallest of {1,1,3,4,5} = 3
    assert conformal_half_width(errors, alpha=0.5) == pytest.approx(3.0)


def test_half_width_returns_none_when_data_insufficient():
    # n=2, alpha=0.05: k = ceil(3 * 0.95) = 3 > 2 → None
    assert conformal_half_width([1.0, 2.0], alpha=0.05) is None
    assert conformal_half_width([], alpha=0.1) is None


# --- coverage -------------------------------------------------------------


def test_empirical_coverage_counts_within_band():
    errors = [1.0, 2.0, 3.0]
    assert empirical_coverage(errors, 2.5) == pytest.approx(2.0 / 3.0)
    assert empirical_coverage(errors, 3.0) == pytest.approx(1.0)
    assert empirical_coverage(errors, 0.5) == pytest.approx(0.0)


def test_calibration_guarantees_at_least_nominal_coverage():
    rng = np.random.default_rng(42)
    errors = np.abs(rng.normal(0.0, 10.0, size=200)).tolist()
    half = conformal_half_width(errors, alpha=0.1)
    assert half is not None
    # On the calibration set itself, coverage is ≥ 1 − alpha by construction.
    assert empirical_coverage(errors, half) >= 0.9


def test_validation():
    with pytest.raises(ForecastError):
        conformal_half_width([1.0], alpha=0.0)
    with pytest.raises(ForecastError):
        conformal_half_width([1.0], alpha=1.0)
    with pytest.raises(ForecastError):
        conformal_half_width([1.0], alpha=-0.1)
    with pytest.raises(ForecastError):
        empirical_coverage([], 1.0)
