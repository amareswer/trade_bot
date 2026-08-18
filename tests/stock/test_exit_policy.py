"""ExitPolicy — asymmetric confidence bars for position-book exits.

Motivating incident (2026-07-10): AC.TO was held while the AI issued SELL
verdicts at 58% and 60% confidence on consecutive cycles. The old single
65% bar (shared with BUY) ignored both — the dashboard showed SELL while
the bot did nothing. ExitPolicy fixes this two ways:
  1. A held position exits on a single SELL >= min_confidence_sell (55).
  2. Or on `streak_cycles` consecutive SELLs each >= streak_min_conf (2 @ 50).
"""

from stock_bot.execution.exit_policy import ExitPolicy


def _policy() -> ExitPolicy:
    # Mirrors the stock_bot/.env defaults (55 / 50 / 2)
    return ExitPolicy(min_confidence_sell=55, streak_min_conf=50, streak_cycles=2)


# ── Single-verdict bar ─────────────────────────────────────────────────────

def test_sell_at_exit_bar_exits_held():
    p = _policy()
    d = p.decide("AC.TO", "SELL", 55, held=True)
    assert d.should_exit
    assert "exit bar" in d.reason


def test_sell_below_both_bars_does_not_exit():
    p = _policy()
    d = p.decide("AC.TO", "SELL", 49, held=True)
    assert not d.should_exit
    assert d.streak == 0  # below streak_min_conf → doesn't even count


def test_high_confidence_sell_on_flat_symbol_is_ignored():
    p = _policy()
    d = p.decide("AMD", "SELL", 90, held=False)
    assert not d.should_exit


# ── Streak rule ────────────────────────────────────────────────────────────

def test_acto_incident_two_weak_sells_exit_on_second():
    """Regression: the exact 2026-07-10 sequence — SELL 60% then SELL 58%."""
    p = _policy()
    first = p.decide("AC.TO", "SELL", 60, held=True)
    assert first.should_exit  # 60 >= 55 — already exits under the new bar
    # Now the pure streak path, both readings below the 55 bar:
    p2 = _policy()
    a = p2.decide("AC.TO", "SELL", 52, held=True)
    assert not a.should_exit and a.streak == 1
    b = p2.decide("AC.TO", "SELL", 53, held=True)
    assert b.should_exit and b.streak == 2
    assert "streak" in b.reason


def test_hold_verdict_breaks_streak():
    p = _policy()
    p.decide("NVDA", "SELL", 52, held=True)
    p.decide("NVDA", "HOLD", 48, held=True)
    d = p.decide("NVDA", "SELL", 52, held=True)
    assert not d.should_exit and d.streak == 1


def test_buy_verdict_breaks_streak():
    p = _policy()
    p.decide("NVDA", "SELL", 52, held=True)
    p.decide("NVDA", "BUY", 70, held=True)
    d = p.decide("NVDA", "SELL", 52, held=True)
    assert not d.should_exit and d.streak == 1


def test_weak_sell_below_streak_min_conf_resets_streak():
    p = _policy()
    p.decide("NVDA", "SELL", 52, held=True)
    p.decide("NVDA", "SELL", 40, held=True)  # too weak — resets, not neutral
    d = p.decide("NVDA", "SELL", 52, held=True)
    assert not d.should_exit and d.streak == 1


def test_streak_tracks_while_flat_then_fires_when_held():
    # Streak accumulates from verdicts even before the position exists in the
    # book (e.g. bought mid-streak); only `held` gates the actual exit.
    p = _policy()
    p.decide("TSLA", "SELL", 52, held=False)
    d = p.decide("TSLA", "SELL", 52, held=True)
    assert d.should_exit and d.streak == 2


def test_streaks_are_per_symbol():
    p = _policy()
    p.decide("AC.TO", "SELL", 52, held=True)
    d = p.decide("DLTR", "SELL", 52, held=True)
    assert not d.should_exit and d.streak == 1


def test_clear_resets_streak_after_position_closed():
    p = _policy()
    p.decide("AC.TO", "SELL", 52, held=True)
    p.clear("AC.TO")
    d = p.decide("AC.TO", "SELL", 52, held=True)
    assert not d.should_exit and d.streak == 1


def test_symbol_case_insensitive():
    p = _policy()
    p.decide("ac.to", "SELL", 52, held=True)
    d = p.decide("AC.TO", "SELL", 52, held=True)
    assert d.should_exit and d.streak == 2
