"""
News headline sentiment scorer — no API key, no extra network call.

Scores the headlines already fetched by news_fetcher.py using a phrase-aware
keyword approach with negation detection. Returns a SentimentData with
source="news_sentiment".

Negation window: if a negation word appears within 3 tokens before a keyword,
the keyword's polarity is flipped (e.g. "did not beat" → negative).
Multi-word phrases are matched before single-token fallback so that
"price target cut" correctly scores negative, not positive.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Ordered from most-specific (phrases) to least-specific (single tokens).
# Each tuple is (phrase_pattern, polarity) where polarity is +1 or -1.
# Patterns are matched case-insensitively against the full headline.
_PHRASE_RULES: list[tuple[str, int]] = [
    # Negative phrases that contain positive words (must come first)
    (r"\bprice target (cut|lower|slash|reduc)",   -1),
    (r"\brating (cut|lower|downgrad)",             -1),
    (r"\bearnings miss",                           -1),
    (r"\brevenue miss",                            -1),
    (r"\bguidance (cut|lower|slash|reduc|miss)",   -1),
    (r"\bprofit warning",                          -1),
    (r"\bjob (cut|loss|eliminat)",                 -1),
    (r"\bshare (sale|selling|dilut)",              -1),
    (r"\bno (dividend|growth|profit)",             -1),
    (r"\bfail(ed|s)? to (beat|meet|grow)",         -1),
    # Positive phrases
    (r"\bprice target (rais|hike|increas|boost)",  +1),
    (r"\bearnings beat",                           +1),
    (r"\brevenue beat",                            +1),
    (r"\bguidance (rais|increas|lift)",            +1),
    (r"\brecord (revenue|profit|earnings|sales)",  +1),
    (r"\bbuy(back| back)?\b",                      +1),
    (r"\bshare (buyback|repurchas)",               +1),
    (r"\bdividend (increas|rais|hike)",            +1),
]

# Single-token fallbacks (used only when no phrase rule matched in a headline)
_POSITIVE_TOKENS = frozenset({
    "beat", "bullish", "upgrade", "strong", "growth", "rally",
    "surge", "gain", "outperform", "record", "lifted", "rebound",
    "recovery", "profit", "dividend", "buyback",
})
_NEGATIVE_TOKENS = frozenset({
    "miss", "bearish", "downgrade", "weak", "decline", "fell",
    "cut", "loss", "below", "concern", "risk", "drop",
    "probe", "lawsuit", "recall", "fraud", "warn", "halt",
    "delay", "disappointing", "disappoint", "deficit",
})
_NEGATION_WORDS = frozenset({
    "not", "no", "never", "without", "fail", "failed", "fails",
    "didn't", "doesn't", "wasn't", "hasn't", "won't", "cannot", "can't",
})
_NEGATION_WINDOW = 3  # tokens before keyword that may negate it


@dataclass
class SentimentData:
    score:      float
    label:      str
    post_count: int                     # number of headlines scored
    source:     str = "news_sentiment"
    top_posts:  list[str] = field(default_factory=list)
    confidence: float = 0.0             # 0–1; 5+ headlines = 1.0


def _label(score: float) -> str:
    if score > 0.05:
        return "POSITIVE"
    if score < -0.05:
        return "NEGATIVE"
    return "NEUTRAL"


def _score_headline(title: str) -> int:
    """
    Return net sentiment score (+N/-N/0) for a single headline.

    Phrase rules are applied first. Any phrase match contributes its polarity
    and skips token-level scoring for that headline to avoid double-counting.
    Token-level scoring applies negation detection within a sliding window.
    """
    text = title.lower()
    phrase_score = 0
    matched_phrase = False
    for pattern, polarity in _PHRASE_RULES:
        if re.search(pattern, text):
            phrase_score += polarity
            matched_phrase = True

    if matched_phrase:
        return phrase_score

    # Token-level scoring with negation window
    words = re.findall(r"[a-z']+", text)
    token_score = 0
    for i, word in enumerate(words):
        polarity = 0
        if word in _POSITIVE_TOKENS:
            polarity = +1
        elif word in _NEGATIVE_TOKENS:
            polarity = -1
        if polarity == 0:
            continue
        # Check for a negation word in the preceding window
        window_start = max(0, i - _NEGATION_WINDOW)
        if any(words[j] in _NEGATION_WORDS for j in range(window_start, i)):
            polarity = -polarity
        token_score += polarity

    return token_score


def score_headlines(news_items) -> SentimentData:
    """
    Score a list of NewsItem objects. Returns SentimentData with score in [-1, +1].
    """
    if not news_items:
        return SentimentData(score=0.0, label="NEUTRAL", post_count=0)

    raw_scores = [_score_headline(item.title) for item in news_items]
    total_pos = sum(s for s in raw_scores if s > 0)
    total_neg = sum(-s for s in raw_scores if s < 0)

    denom = total_pos + total_neg
    K = 4  # Laplace smoothing — prevents ±1.00 from a single keyword hit
    score = round((total_pos - total_neg) / (denom + K), 3) if denom > 0 else 0.0
    confidence = round(min(1.0, len(news_items) / 5), 2)

    logger.debug(
        "News sentiment: %d headlines, +%d/-%d hits, score=%.3f, confidence=%.2f",
        len(news_items), total_pos, total_neg, score, confidence,
    )
    return SentimentData(
        score      = score,
        label      = _label(score),
        post_count = len(news_items),
        source     = "news_sentiment",
        confidence = confidence,
    )
