"""
News headline fetcher via RSS.

Pulls from Yahoo Finance and Google News RSS for a given symbol.
Returns up to 5 deduplicated NewsItem objects, newest first.
All errors are caught and logged — never raises to the caller.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import feedparser

_news_cache: dict[str, tuple[list, float]] = {}
_NEWS_TTL   = 600  # 10 minutes — news doesn't change every cycle

logger = logging.getLogger(__name__)

_YAHOO_RSS  = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
_GOOGLE_RSS = "https://news.google.com/rss/search?q={ticker}+stock&hl=en-CA&gl=CA&ceid=CA:en"
_USER_AGENT = "Mozilla/5.0 (compatible; StockBot/1.0)"
_MAX_ITEMS  = 5


@dataclass
class NewsItem:
    title:     str
    source:    str
    published: datetime
    url:       str
    note:      str = ""  # non-empty when filter fell back to unfiltered results


def _clean_ticker(symbol: str) -> str:
    """Strip exchange suffixes so RSS URLs work: SHOP.TO → SHOP."""
    return symbol.split(".")[0]


def _parse_feed(url: str, source_label: str) -> list[NewsItem]:
    try:
        feed = feedparser.parse(url, agent=_USER_AGENT)
        items: list[NewsItem] = []
        for entry in feed.entries:
            try:
                if entry.get("published_parsed"):
                    dt = datetime(*entry.published_parsed[:6])
                else:
                    dt = datetime.now()
                items.append(NewsItem(
                    title     = entry.get("title", "").strip(),
                    source    = source_label,
                    published = dt,
                    url       = entry.get("link", ""),
                ))
            except Exception:
                continue
        return items
    except Exception as exc:
        logger.warning("RSS fetch failed (%s): %s", source_label, exc)
        return []


def _is_relevant(title: str, ticker: str, company_name: str) -> bool:
    """Return True if the headline mentions the ticker or company name."""
    t = title.lower()
    return ticker.lower() in t or (company_name and company_name.lower() in t)


def fetch_news(symbol: str, company_name: str = "") -> list[NewsItem]:
    """
    Fetch up to 5 recent headlines for `symbol` from Yahoo Finance + Google News.
    Results are cached for 10 minutes — news doesn't change between bot cycles.
    """
    cached = _news_cache.get(symbol)
    if cached is not None:
        headlines, ts = cached
        if time.time() - ts < _NEWS_TTL:
            return headlines

    ticker = _clean_ticker(symbol)
    yahoo  = _parse_feed(_YAHOO_RSS.format(ticker=ticker),  "Yahoo Finance")
    google = _parse_feed(_GOOGLE_RSS.format(ticker=ticker), "Google News")

    # Deduplicate by title, sort newest first, cap at _MAX_ITEMS
    seen: set[str] = set()
    combined: list[NewsItem] = []
    for item in sorted(yahoo + google, key=lambda x: x.published, reverse=True):
        key = item.title.lower()[:60]
        if key not in seen:
            seen.add(key)
            combined.append(item)
        if len(combined) >= _MAX_ITEMS:
            break

    # Filter to headlines that reference the symbol or company
    relevant = [n for n in combined if _is_relevant(n.title, ticker, company_name)]

    if len(relevant) >= 2:
        logger.debug(
            "News for %s: %d/%d headlines relevant after filtering",
            symbol, len(relevant), len(combined),
        )
        result = relevant[:_MAX_ITEMS]
        _news_cache[symbol] = (result, time.time())
        return result

    # Fewer than 2 relevant → fall back to unfiltered so the card isn't empty
    name_clause = f" or '{company_name}'" if company_name else ""
    fallback_note = (
        f"No headlines matched '{ticker}'{name_clause} — showing general results"
    )
    logger.debug(
        "News for %s: only %d relevant headline(s) — returning unfiltered %d (%s)",
        symbol, len(relevant), len(combined), fallback_note,
    )
    for item in combined:
        item.note = fallback_note
    _news_cache[symbol] = (combined, time.time())
    return combined
