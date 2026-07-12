from datetime import date

from agents.risk_manager import assess_risk
from data.models import Decision, RiskVerdict, TraderDecision


def _make_decision(**overrides) -> TraderDecision:
    """Helper to create a TraderDecision with sensible defaults."""
    defaults = dict(
        ticker="AAPL",
        date=date(2026, 4, 14),
        decision=Decision.BUY,
        confidence=0.78,
        entry_zone=[185.00, 187.00],
        stop_loss=182.00,
        targets=[196.00],
        invalidation="Close below 180",
        holding_period_days=5,
        thesis="Test thesis",
    )
    defaults.update(overrides)
    return TraderDecision(**defaults)


# ─── Approval Tests ──────────────────────────────────────

def test_approved_standard_buy():
    d = _make_decision()
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.APPROVED
    assert r.position_size_pct > 0


def test_position_size_low_confidence():
    """0.65-0.75 confidence → 2% position."""
    d = _make_decision(confidence=0.70)
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.APPROVED
    assert r.position_size_pct == 0.02


def test_position_size_mid_confidence():
    """0.75-0.85 confidence → 3% position."""
    d = _make_decision(confidence=0.80)
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.APPROVED
    assert r.position_size_pct == 0.03


def test_position_size_high_confidence():
    """0.85+ confidence → 5% position."""
    d = _make_decision(confidence=0.90)
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.APPROVED
    assert r.position_size_pct == 0.05


def test_size_halved_low_rr():
    """R/R < 2.0 → size halved."""
    # entry_mid = 186, stop = 182, target = 190
    # reward = 4, risk = 4, R/R = 1.0 → halve
    d = _make_decision(
        confidence=0.80,
        entry_zone=[185.0, 187.0],
        stop_loss=182.0,
        targets=[190.0],
    )
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.APPROVED
    assert r.position_size_pct == 0.015  # 3% halved
    assert len(r.adjustments) == 1


def test_rr_ratio_calculated_correctly():
    """Verify R/R math: (target - entry_mid) / (entry_mid - stop)."""
    # entry_mid = 186, stop = 182, target = 198
    # reward = 12, risk = 4, R/R = 3.0
    d = _make_decision(
        entry_zone=[185.0, 187.0],
        stop_loss=182.0,
        targets=[198.0],
    )
    r = assess_risk(d)
    assert r.reward_risk_ratio == 3.0


# ─── Rejection Tests ─────────────────────────────────────

def test_reject_low_confidence():
    d = _make_decision(confidence=0.55)
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.REJECTED
    assert r.position_size_pct == 0.0
    assert any("Confidence" in reason for reason in r.rejection_reasons)


def test_reject_wide_stop():
    """Stop > 5% from entry → reject."""
    # entry_mid = 186, stop = 170 → 8.6% away
    d = _make_decision(stop_loss=170.0)
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.REJECTED
    assert any("Stop loss" in reason for reason in r.rejection_reasons)


def test_reject_long_hold():
    d = _make_decision(holding_period_days=15)
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.REJECTED
    assert any("Holding period" in reason for reason in r.rejection_reasons)


def test_reject_wide_entry_zone():
    """Entry zone width > 3% → reject."""
    # entry zone [180, 195] → width 15/187.5 = 8%
    d = _make_decision(entry_zone=[180.0, 195.0])
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.REJECTED
    assert any("Entry zone" in reason for reason in r.rejection_reasons)


def test_reject_negative_rr():
    """Target below entry for a BUY → R/R < 1 → reject."""
    d = _make_decision(
        entry_zone=[185.0, 187.0],
        stop_loss=184.0,
        targets=[185.0],
    )
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.REJECTED
    assert any("Invalid BUY bracket" in reason for reason in r.rejection_reasons)


def test_reject_buy_stop_above_entry_with_explicit_reason():
    d = _make_decision(
        decision=Decision.BUY,
        entry_zone=[185.0, 187.0],
        stop_loss=190.0,
        targets=[198.0],
    )
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.REJECTED
    assert any("Invalid BUY bracket" in reason for reason in r.rejection_reasons)


def test_reject_sell_stop_below_entry_with_explicit_reason():
    d = _make_decision(
        decision=Decision.SELL,
        entry_zone=[185.0, 187.0],
        stop_loss=182.0,
        targets=[176.0],
    )
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.REJECTED
    assert any("Invalid SELL bracket" in reason for reason in r.rejection_reasons)


def test_reject_multiple_reasons():
    """Multiple violations → multiple rejection reasons."""
    d = _make_decision(
        confidence=0.50,
        holding_period_days=20,
        entry_zone=[170.0, 200.0],
    )
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.REJECTED
    assert len(r.rejection_reasons) >= 3


# ─── HOLD Tests ──────────────────────────────────────────

def test_hold_has_zero_position_size():
    """HOLD is a valid no-action signal, not a sized trade."""
    d = _make_decision(decision=Decision.HOLD, confidence=0.80)
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.APPROVED
    assert r.position_size_pct == 0.0
    assert r.reward_risk_ratio == 0.0
    assert r.rejection_reasons == []


def test_hold_ignores_trade_geometry():
    """Trader may still include placeholder trade levels for HOLD."""
    d = _make_decision(
        decision=Decision.HOLD,
        confidence=0.80,
        entry_zone=[185.0, 187.0],
        stop_loss=250.0,
        targets=[100.0],
    )
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.APPROVED
    assert r.position_size_pct == 0.0


# ─── SELL (short) Tests ──────────────────────────────────

def test_sell_rr_calculated_correctly():
    """For shorts: reward = entry - target, risk = stop - entry."""
    # entry_mid = 186, stop = 192, target = 176
    # reward = 10, risk = 6, R/R = 1.67
    d = _make_decision(
        decision=Decision.SELL,
        entry_zone=[185.0, 187.0],
        stop_loss=192.0,
        targets=[176.0],
    )
    r = assess_risk(d)
    assert r.reward_risk_ratio == 1.67


def test_sell_approved_good_rr():
    # entry_mid = 186, stop = 190, target = 170
    # reward = 16, risk = 4, R/R = 4.0
    d = _make_decision(
        decision=Decision.SELL,
        confidence=0.80,
        entry_zone=[185.0, 187.0],
        stop_loss=190.0,
        targets=[170.0],
    )
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.APPROVED
    assert r.reward_risk_ratio == 4.0


# ─── Edge Cases ──────────────────────────────────────────

def test_confidence_at_boundary():
    """Exactly 0.65 → approved (min_confidence is exclusive lower bound)."""
    d = _make_decision(confidence=0.65)
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.APPROVED


def test_confidence_just_below_boundary():
    d = _make_decision(confidence=0.6499)
    r = assess_risk(d)
    assert r.verdict == RiskVerdict.REJECTED
