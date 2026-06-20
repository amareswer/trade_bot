"""
Prompt builder for the stock bot AI engine.

Assembles a structured analysis prompt from price data, indicators,
and the ResearchReport produced by the Phase 2 research engine.
Target: < 800 tokens (3 headlines, concise sections).
"""
from __future__ import annotations

from stock_bot.research.aggregator import ResearchReport


def _rsi_note(rsi: float | None) -> str:
    if rsi is None:
        return "unavailable"
    if rsi >= 75:
        return "strongly overbought ⚠"
    if rsi >= 70:
        return "overbought ⚠"
    if rsi >= 55:
        return "leaning overbought"
    if rsi <= 25:
        return "strongly oversold ⚠"
    if rsi <= 30:
        return "oversold ⚠"
    if rsi <= 45:
        return "leaning oversold"
    return "neutral"


def _macd_note(macd_line: float | None, macd_signal: float | None) -> str:
    if macd_line is None or macd_signal is None:
        return "unavailable"
    diff = macd_line - macd_signal
    if abs(diff) < 0.001 * max(abs(macd_line), abs(macd_signal), 0.01):
        return "flat"
    return "bullish cross" if diff > 0 else "bearish cross"


def build_prompt(
    symbol:          str,
    candle,                           # stock_bot.data.price_feed.Candle
    indicators:      dict,            # keys: rsi, trend, adx, macd_line, macd_signal
    research:        ResearchReport,
    stop_loss_pct:   float = 0.05,
    take_profit_pct: float = 0.12,
) -> str:
    price       = candle.close
    sl_price    = round(price * (1 - stop_loss_pct),   2)
    tp_price    = round(price * (1 + take_profit_pct), 2)
    sl_pct_str  = f"{stop_loss_pct  * 100:.1f}%"
    tp_pct_str  = f"{take_profit_pct * 100:.1f}%"

    rsi         = indicators.get("rsi")
    trend       = indicators.get("trend") or "NEUTRAL"
    macd_line   = indicators.get("macd_line")
    macd_signal = indicators.get("macd_signal")

    rsi_str   = f"{rsi:.1f}" if rsi is not None else "n/a"
    macd_l_str = f"{macd_line:+.3f}" if macd_line is not None else "n/a"
    macd_s_str = f"{macd_signal:+.3f}" if macd_signal is not None else "n/a"

    # News block — cap at 3 headlines to stay under token budget
    if research.news:
        news_block = "\n".join(
            f"  • {n.title[:90]}" for n in research.news[:3]
        )
    else:
        news_block = "  No news available"

    # Earnings
    e = research.earnings
    earnings_str   = e.earnings_note or "No data"
    next_earn_str  = str(e.next_earnings_date) if e.next_earnings_date else "unknown"

    # News sentiment
    s = research.sentiment
    stocktwits_str = (
        f"{s.score:+.3f} ({s.label}, confidence={s.confidence:.0%}) | {s.post_count} headlines scored"
        if s.post_count > 0
        else "no headlines"
    )

    # Market trends
    if research.market_trends_score is None:
        trends_str = "unavailable (rate limited — ignore this cycle)"
    else:
        mts        = research.market_trends_score
        trends_str = f"{mts}/100" + (" 🔥 high interest" if mts > 70 else "")

    # Volume vs 20-day average
    vol_ratio = getattr(candle, "volume_ratio", None)
    if vol_ratio is not None:
        if vol_ratio >= 2.0:
            vol_note = f"{vol_ratio:.1f}× avg ⚠ unusually high volume"
        elif vol_ratio >= 1.3:
            vol_note = f"{vol_ratio:.1f}× avg — above average"
        elif vol_ratio <= 0.5:
            vol_note = f"{vol_ratio:.1f}× avg ⚠ low volume — treat signals cautiously"
        else:
            vol_note = f"{vol_ratio:.1f}× avg — normal"
    else:
        vol_note = "unavailable"

    # Fear & Greed
    fg = research.fear_greed

    ipo_note = (
        "\n⚠ NOTE: Recent IPO — limited price history. "
        "Weight news, sentiment, and fundamentals more heavily than technicals.\n"
        if trend == "NEW IPO" else ""
    )

    return f"""You are an expert stock analyst covering both US and Canadian markets.
Analyze the following data for {symbol} and give a clear trading recommendation.
{ipo_note}
=== PRICE & TECHNICALS ===
Current Price: ${price:,.2f}
Volume vs 20-day avg: {vol_note}
RSI (14): {rsi_str} → {_rsi_note(rsi)}
Trend (EMA 9/21): {trend}
MACD: {macd_l_str} / Signal: {macd_s_str} → {_macd_note(macd_line, macd_signal)}

Pre-calculated risk levels (use these exactly — do not invent alternatives):
  Stop loss:   ${sl_price} ({sl_pct_str} below current price)
  Take profit: ${tp_price} ({tp_pct_str} above current price)

=== NEWS (last 3 headlines) ===
{news_block}

=== NEWS SENTIMENT ===
Score: {stocktwits_str}

=== MARKET SEARCH INTEREST (7-day Google Trends) ===
Interest: {trends_str}

=== EARNINGS ===
{earnings_str}
Next earnings: {next_earn_str}

=== MARKET SENTIMENT ===
CNN Fear & Greed Index: {fg.score} — {fg.label}

=== YOUR TASK ===
Respond ONLY with a JSON object. No markdown, no explanation outside the JSON.
Use this exact structure (stop_loss and target_price are pre-calculated — copy them verbatim):
{{
  "signal": "BUY" | "SELL" | "HOLD",
  "confidence": <integer 0-100>,
  "target_price": {tp_price},
  "stop_loss": {sl_price},
  "trading_style": "DAY" | "SWING" | "LONGTERM",
  "reasoning": "<2-4 sentences explaining the recommendation>"
}}

Rules:
- confidence below 55 → always output HOLD regardless of signal
- target_price and stop_loss in same currency as current price
- trading_style: DAY if RSI extreme + momentum, LONGTERM if earnings/fundamentals driven, SWING otherwise
- reasoning must reference at least 2 data points from above
- never recommend BUY if RSI > 75
- never recommend SELL if RSI < 25
- low volume moves (volume <0.5× average) should lower your confidence by 10-15 points"""
