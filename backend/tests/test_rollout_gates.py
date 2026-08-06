"""Unit tests for Rollout Gates (Phase 15)."""
import sys, os
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.rollout_gates import (
    evaluate_rollout, emergency_env_reset,
    FlagStatus, RolloutStatusReport, _PROMOTION_CRITERIA,
)


# ─────────────────────────────────────────────────────────────────────────────
# Emergency env reset
# ─────────────────────────────────────────────────────────────────────────────

def test_emergency_env_reset_covers_all_flags():
    envs = emergency_env_reset()
    # Every phase flag must appear
    for flag_name in _PROMOTION_CRITERIA.keys():
        assert flag_name.upper() in envs, f"missing {flag_name}"
    # Shadow mode is set to true (safety valve)
    assert envs["XAUUSD_MARKET_INTEL_SHADOW_MODE"] == "true"


def test_emergency_env_reset_all_values_false_or_true():
    """Only two values allowed. Never sets credentials or lot sizing."""
    envs = emergency_env_reset()
    for k, v in envs.items():
        assert v in ("false", "true"), f"{k}={v} should be false/true"
        assert "MT5" not in k, f"emergency reset must not touch MT5 flag: {k}"
        assert "LOT" not in k, f"emergency reset must not touch lot sizing: {k}"
        assert "PASSWORD" not in k, f"emergency reset must not touch credentials: {k}"


# ─────────────────────────────────────────────────────────────────────────────
# Promotion criteria structure
# ─────────────────────────────────────────────────────────────────────────────

def test_every_phase_2_to_14_has_criteria():
    """Each Phase 2/3/4/5/6/7/8/9/10/11/13/14 flag documented."""
    phases_covered = {c["phase"] for c in _PROMOTION_CRITERIA.values()}
    expected = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14}
    assert expected.issubset(phases_covered), \
        f"phases missing criteria: {expected - phases_covered}"


def test_every_criterion_has_all_required_fields():
    for name, meta in _PROMOTION_CRITERIA.items():
        for field in ("requires", "risk", "gate_flag", "phase"):
            assert field in meta, f"{name} missing {field}"
        assert isinstance(meta["phase"], int)
        # Risk field must open with a severity keyword the operator can grep
        risk = meta["risk"].lower()
        assert any(sev in risk for sev in ("low", "medium", "high")), \
            f"{name} risk field {meta['risk']!r} missing severity keyword"


def test_intel_alert_flag_has_gate_flag():
    """The user-visible flag must reference shadow_mode as its gate."""
    meta = _PROMOTION_CRITERIA["xauusd_market_intelligence_telegram_enabled"]
    assert meta["gate_flag"] == "xauusd_market_intel_shadow_mode"


# ─────────────────────────────────────────────────────────────────────────────
# evaluate_rollout behaviour with mocked DB
# ─────────────────────────────────────────────────────────────────────────────

def _mock_db_with_expansions(count=10):
    db = MagicMock()
    def _fetchone(*args, **kwargs):
        # For any COUNT(*) query, return count. For MIN/MAX return None.
        return (count,)
    db.execute.return_value.fetchone.side_effect = _fetchone
    return db


def test_evaluate_rollout_returns_status_report():
    db = _mock_db_with_expansions(count=10)
    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"data_quality_score": 100}
        report = evaluate_rollout(db)
    assert isinstance(report, RolloutStatusReport)
    assert report.generated_at is not None
    # Every criterion produces one flag status
    assert len(report.flags) == len(_PROMOTION_CRITERIA)
    # Counts add up
    total = report.ready_count + report.not_ready_count + report.unable_to_judge
    assert total == len(report.flags)
    # Kill switch hint present
    assert "emergency" in report.kill_switch_hint.lower() or \
           "disable" in report.kill_switch_hint.lower()


def test_evaluate_rollout_marks_canonical_ready_when_dq_high():
    db = _mock_db_with_expansions(count=10)
    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"data_quality_score": 95}
        report = evaluate_rollout(db)
    canonical = next(f for f in report.flags
                      if f.flag == "xauusd_canonical_data_enabled")
    assert canonical.ready is True
    assert "95" in canonical.ready_reason


def test_evaluate_rollout_marks_canonical_not_ready_when_dq_low():
    db = _mock_db_with_expansions(count=10)
    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"data_quality_score": 60}
        report = evaluate_rollout(db)
    canonical = next(f for f in report.flags
                      if f.flag == "xauusd_canonical_data_enabled")
    assert canonical.ready is False


def test_evaluate_rollout_replay_not_ready_when_zero_expansions():
    db = _mock_db_with_expansions(count=0)
    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"data_quality_score": 100}
        report = evaluate_rollout(db)
    replay = next(f for f in report.flags
                   if f.flag == "xauusd_replay_validation_enabled")
    assert replay.ready is False


def test_evaluate_rollout_replay_ready_when_enough_expansions():
    db = _mock_db_with_expansions(count=15)
    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"data_quality_score": 100}
        report = evaluate_rollout(db)
    replay = next(f for f in report.flags
                   if f.flag == "xauusd_replay_validation_enabled")
    assert replay.ready is True


# ─────────────────────────────────────────────────────────────────────────────
# Currently-enabled reads config
# ─────────────────────────────────────────────────────────────────────────────

def test_currently_enabled_reads_config_defaults():
    """All Phase flags default False → every flag.currently_enabled = False."""
    db = _mock_db_with_expansions(count=10)
    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"data_quality_score": 100}
        report = evaluate_rollout(db)
    for f in report.flags:
        assert f.currently_enabled is False, \
            f"{f.flag} unexpectedly enabled by default"


def test_shadow_mode_reported_as_gate_currently_true():
    """market_intel flag's gate = shadow_mode, which defaults True."""
    db = _mock_db_with_expansions(count=10)
    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"data_quality_score": 100}
        report = evaluate_rollout(db)
    intel = next(f for f in report.flags
                  if f.flag == "xauusd_market_intelligence_telegram_enabled")
    assert intel.gate_flag == "xauusd_market_intel_shadow_mode"
    assert intel.gate_currently is True


# ─────────────────────────────────────────────────────────────────────────────
# Serialization
# ─────────────────────────────────────────────────────────────────────────────

def test_report_to_dict_shape():
    db = _mock_db_with_expansions(count=10)
    with patch("services.data_freshness.check_freshness") as mock_f:
        mock_f.return_value = {"data_quality_score": 100}
        report = evaluate_rollout(db)
    d = report.to_dict()
    for k in ("generated_at", "flags", "ready_count", "not_ready_count",
               "unable_to_judge", "kill_switch_hint"):
        assert k in d
    assert isinstance(d["flags"], list)


if __name__ == "__main__":
    import subprocess
    subprocess.run(["python", "-m", "pytest", __file__, "-v"], check=False)
