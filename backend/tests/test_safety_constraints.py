from app.risk import RiskConfig
from app.safety import evaluate_constraints, list_constraints


def _cfg(**kwargs) -> RiskConfig:
    defaults = dict(
        budget_cap=50_000.0,
        max_position_pct=0.05,
        daily_loss_cap_pct=0.02,
        max_drawdown_pct=0.10,
        default_stop_loss_pct=0.03,
        max_stop_loss_pct=0.10,
    )
    defaults.update(kwargs)
    return RiskConfig(**defaults)


def _keys(cfg: RiskConfig) -> set[str]:
    return {v.key for v in evaluate_constraints(cfg)}


def test_list_constraints_covers_registry():
    items = list_constraints()
    keys = {i["key"] for i in items}
    assert {
        "pdt_rule",
        "per_trade_spread_floor",
        "options_budget_floor",
        "daily_cap_smaller_than_one_stop",
        "default_stop_above_daily_cap",
        "drawdown_redundant",
    } <= keys
    for i in items:
        assert i["severity"] in {"error", "warn", "info"}
        assert i["title"] and i["description"] and i["remedy"]


def test_healthy_config_has_no_violations():
    cfg = _cfg(
        budget_cap=50_000.0,
        max_position_pct=0.05,  # per_trade_max = $2,500
        daily_loss_cap_pct=0.02,
        max_drawdown_pct=0.10,
        default_stop_loss_pct=0.02,
        max_stop_loss_pct=0.05,
    )
    assert _keys(cfg) == set()


def test_pdt_rule_triggers_below_25k():
    cfg = _cfg(budget_cap=10_000.0, max_position_pct=0.10)
    assert "pdt_rule" in _keys(cfg)


def test_pdt_rule_quiet_at_or_above_25k():
    cfg = _cfg(budget_cap=25_000.0, max_position_pct=0.05)
    assert "pdt_rule" not in _keys(cfg)


def test_spread_floor_triggers_on_tiny_per_trade():
    # per_trade_max = $10 — way under the $50 floor
    cfg = _cfg(budget_cap=100.0, max_position_pct=0.10)
    assert "per_trade_spread_floor" in _keys(cfg)


def test_spread_floor_quiet_above_50():
    cfg = _cfg(budget_cap=2_000.0, max_position_pct=0.05)  # $100
    assert "per_trade_spread_floor" not in _keys(cfg)


def test_options_floor_triggers_under_500():
    cfg = _cfg(budget_cap=5_000.0, max_position_pct=0.05)  # $250
    assert "options_budget_floor" in _keys(cfg)


def test_options_floor_quiet_at_500():
    cfg = _cfg(budget_cap=10_000.0, max_position_pct=0.05)  # $500
    assert "options_budget_floor" not in _keys(cfg)


def test_daily_cap_below_single_stop_triggers():
    # worst stop = 2500 * 0.05 = 125; daily cap = 50_000 * 0.002 = 100
    cfg = _cfg(
        budget_cap=50_000.0,
        max_position_pct=0.05,
        max_stop_loss_pct=0.05,
        daily_loss_cap_pct=0.002,
        max_drawdown_pct=0.10,
    )
    assert "daily_cap_smaller_than_one_stop" in _keys(cfg)


def test_default_stop_above_daily_cap_triggers():
    cfg = _cfg(
        default_stop_loss_pct=0.05,
        max_stop_loss_pct=0.05,
        daily_loss_cap_pct=0.02,
    )
    assert "default_stop_above_daily_cap" in _keys(cfg)


def test_drawdown_redundant_triggers_when_equal():
    cfg = _cfg(daily_loss_cap_pct=0.05, max_drawdown_pct=0.05)
    assert "drawdown_redundant" in _keys(cfg)


def test_drawdown_redundant_quiet_when_drawdown_wider():
    cfg = _cfg(daily_loss_cap_pct=0.02, max_drawdown_pct=0.10)
    assert "drawdown_redundant" not in _keys(cfg)
