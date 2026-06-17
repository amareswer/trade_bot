"""
News headline sentiment scorer — no API key, no extra network call.

Scores the headlines already fetched by news_fetcher.py using a keyword
approach. Returns a SentimentData with source="news_sentiment".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_POSITIVE = frozenset({
    "beat", "bullish", "buy", "upgrade", "strong", "growth",
    "rally", "surge", "gain", "outperform", "raise", "target", "record",
})
_NEGATIVE = frozenset({
    "miss", "bearish", "sell", "downgrade", "weak", "decline",
    "fall", "cut", "loss", "below", "concern", "risk", "drop",
    "probe", "lawsuit",
})


@dataclass
class SentimentData:
    score:      float
    label:      str
    post_count: int                     # number of headlines scored
    source:     str = "news_sentiment"
    top_posts:  list[str] = field(default_factory=list)


def _label(score: float) -> str:
    if score > 0.05:
        return "POSITIVE"
    if score < -0.05:
        return "NEGATIVE"
    return "NEUTRAL"


def score_headlines(news_items) -> SentimentData:
    """
    Score a list of NewsItem objects by counting positive/negative keywords
    in each title. Returns SentimentData with score in [-1, +1].
    """
    if not news_items:
        return SentimentData(score=0.0, label="NEUTRAL", post_count=0)

    total_pos = total_neg = 0
    for item in news_items:
        words = item.title.lower().split()
        total_pos += sum(1 for w in words if w in _POSITIVE)
        total_neg += sum(1 for w in words if w in _NEGATIVE)

    denom = total_pos + total_neg
    score = round((total_pos - total_neg) / denom, 3) if denom > 0 else 0.0

    logger.debug(
        "News sentiment: %d headlines, +%d/-%d hits, score=%.3f",
        len(news_items), total_pos, total_neg, score,
    )
    return SentimentData(
        score      = score,
        label      = _label(score),
        post_count = len(news_items),
        source     = "news_sentiment",
    )
