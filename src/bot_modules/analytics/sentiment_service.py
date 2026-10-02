"""VADER sentiment scoring for a single piece of text.

``score_text`` returns a compound score and a coarse emotion label. It runs on
the live message as it arrives, so the score is stored without the text, and on
text fetched from Discord for Word Cloud. There is no batch rescore: the stored
text it once re-read was removed on 2026-09-13.
"""

from __future__ import annotations

import logging

log = logging.getLogger("dungeonkeeper.sentiment")

# Lazy-load VADER so the import cost is only paid when actually scoring
_analyzer = None


def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        try:
            from vaderSentiment.vaderSentiment import (  # type: ignore[import-untyped]
                SentimentIntensityAnalyzer,
            )

            _analyzer = SentimentIntensityAnalyzer()
        except ImportError:
            log.warning("vaderSentiment not installed — sentiment analysis disabled")
            return None
    return _analyzer


def _classify_emotion(compound: float, pos: float, neg: float) -> str:
    """Map VADER scores to a simple emotion category."""
    if compound >= 0.5:
        return "joy"
    if compound >= 0.15:
        return "playful"
    if compound <= -0.5:
        return "anger"
    if compound <= -0.15:
        return "frustration"
    return "neutral"


def score_text(text: str | None) -> tuple[float | None, str | None]:
    """Score a single message and return ``(compound, emotion)`` or ``(None, None)``."""
    if not text or len(text.strip()) < 2:
        return None, None
    analyzer = _get_analyzer()
    if analyzer is None:
        return None, None
    scores = analyzer.polarity_scores(text)
    compound = round(scores["compound"], 4)
    emotion = _classify_emotion(compound, scores["pos"], scores["neg"])
    return compound, emotion
