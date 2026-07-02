"""
Provider-Independent FinBERT Sentiment Engine
----------------------------------------------
Governance Rule #21: FinBERT must not be hard-coupled to any single provider.

Pipeline:
    Any Provider → Normalize → Deduplicate → FinBERT → Scored Articles

FinBERT Failure Governance:
    If FinBERT model fails (OOM, init failure), the scanner continues operating.
    Articles remain available. Sentiment status = keyword fallback.

Data Contract (matches legacy system):
    Returns: (score: float, items: list[dict], source_breakdown: dict)
    Score range: -15 to +15 (expected by analyzer.py)
"""

import re
import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger("screener")

# ──────────────────────────────────────────────────────────────
# FinBERT — lazy load once, thread-safe singleton
# ──────────────────────────────────────────────────────────────
_finbert = None
_finbert_lock = threading.Lock()
_finbert_failed = False  # Permanent flag if model can't load
_FINBERT_BATCH = 32


def _get_finbert():
    """Lazy-load FinBERT model. Returns None if unavailable."""
    global _finbert, _finbert_failed
    if _finbert_failed:
        return None
    if _finbert is None:
        with _finbert_lock:
            if _finbert is None and not _finbert_failed:
                try:
                    from transformers import pipeline
                    import torch

                    if torch.cuda.is_available():
                        device = 0
                        device_name = torch.cuda.get_device_name(0)
                        log.info(f"CUDA GPU detected: {device_name}. Loading FinBERT on GPU (CUDA:0)...")
                    else:
                        device = -1
                        log.info("CUDA GPU not available. Loading FinBERT on CPU...")
                        try:
                            torch.set_num_threads(2)
                            torch.set_num_interop_threads(2)
                            log.info("PyTorch CPU threads limited to 2.")
                        except Exception as thread_exc:
                            log.warning("Could not limit PyTorch CPU threads: %s", thread_exc)

                    _finbert = pipeline(
                        "text-classification",
                        model="ProsusAI/finbert",
                        batch_size=_FINBERT_BATCH,
                        truncation=True,
                        max_length=128,
                        device=device,
                    )
                    log.info("FinBERT loaded OK (provider-independent engine)")
                except Exception as exc:
                    log.warning("FinBERT load failed: %s — using keyword fallback", exc)
                    _finbert_failed = True
                    _finbert = None
    return _finbert


def is_finbert_available() -> bool:
    """Check if FinBERT model is loaded and operational."""
    return _get_finbert() is not None


# ──────────────────────────────────────────────────────────────
# Keyword Fallback (FinBERT Failure Governance)
# ──────────────────────────────────────────────────────────────

def _keyword_sentiment(text: str) -> float:
    """Fast keyword fallback when FinBERT unavailable. Returns -1 to +1."""
    text_l = text.lower()
    pos = ["profit", "growth", "order win", "contract", "acquisition", "beat", "surge",
           "record", "expansion", "buyback", "dividend", "upgrade", "positive", "strong",
           "rally", "outperform", "revenue up", "wins", "awarded", "new order"]
    neg = ["loss", "decline", "miss", "downgrade", "fraud", "penalty", "default",
           "bankruptcy", "layoff", "cut", "below", "weak", "negative",
           "selloff", "crash", "warning", "probe", "fine", "reject"]
    score = sum(1 for w in pos if w in text_l) - sum(1 for w in neg if w in text_l)
    return max(-1.0, min(1.0, score * 0.3))


# ──────────────────────────────────────────────────────────────
# Normalize & Deduplicate
# ──────────────────────────────────────────────────────────────

def normalize_articles(articles: list) -> list:
    """
    Normalize articles from any provider into a standard format.
    
    Input accepts dicts with any of:
        title/headline, score/sentiment_score, source, date/published_at, age_hours
    
    Output: list of dicts with keys: title, source, date, age_hours
    """
    normalized = []
    for art in articles:
        title = art.get("title") or art.get("headline") or ""
        title = title.strip()
        if not title or len(title) < 10:
            continue  # Skip empty or trivially short titles

        normalized.append({
            "title": title,
            "source": art.get("source", "unknown"),
            "date": art.get("date") or art.get("published_at", "")[:10] if art.get("published_at") else "",
            "age_hours": art.get("age_hours", 12.0),
        })
    return normalized


def deduplicate_articles(articles: list) -> list:
    """
    Remove duplicate articles by normalized title.
    Keeps the first occurrence (typically from the higher-priority provider).
    """
    seen = set()
    unique = []
    for art in articles:
        key = re.sub(r'\s+', ' ', art["title"].lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(art)
    return unique


# ──────────────────────────────────────────────────────────────
# Freshness Weight
# ──────────────────────────────────────────────────────────────

def _recency_weight(age_hours: float) -> float:
    """Exponential decay with floor: 0h→1.0, 12h→0.5, 48h→0.2, floor=0.15"""
    return max(0.15, 1 / (1 + age_hours / 12))


# ──────────────────────────────────────────────────────────────
# Core FinBERT Batch Scoring
# ──────────────────────────────────────────────────────────────

def score_articles(articles: list) -> list:
    """
    Score a list of normalized articles with FinBERT (or keyword fallback).
    
    Input: list of dicts with at least 'title' key.
    Output: list of dicts enriched with 'sentiment', 'confidence', 'method' keys.
    
    FinBERT Failure Governance: If model fails, falls back to keywords.
    Articles are never lost — only sentiment method changes.
    """
    if not articles:
        return []

    headlines = [a["title"] for a in articles]
    clf = _get_finbert()

    if clf is not None:
        try:
            results = clf(headlines, truncation=True, max_length=128)
            for art, r in zip(articles, results):
                label = r["label"].lower()
                conf = r["score"]
                if label == "positive":
                    art["sentiment"] = conf
                elif label == "negative":
                    art["sentiment"] = -conf
                else:
                    art["sentiment"] = 0.0
                art["confidence"] = conf
                art["method"] = "finbert"
            return articles
        except Exception as exc:
            log.warning("FinBERT batch scoring failed: %s — falling back to keywords", exc)

    # Keyword fallback
    for art in articles:
        score = _keyword_sentiment(art["title"])
        art["sentiment"] = score
        art["confidence"] = abs(score)
        art["method"] = "keyword"

    return articles


# ──────────────────────────────────────────────────────────────
# News Impact Pipeline
# Raw Sentiment → Confidence → Freshness Weight → Impact Score
# ──────────────────────────────────────────────────────────────

def compute_news_impact(scored_articles: list) -> dict:
    """
    Full News Impact Pipeline (Governance Rule).
    
    Pipeline: Raw Sentiment → Confidence Score → Freshness Weight → Impact Score
    
    Returns the legacy-compatible data contract:
        {
            "score": float (-15 to +15),
            "items": list[dict],
            "source_breakdown": dict,
            "finbert_coverage": float (0-100%),
            "method": str
        }
    """
    if not scored_articles:
        return {
            "score": 0.0,
            "items": [],
            "source_breakdown": {},
            "finbert_coverage": 0.0,
            "method": "none",
        }

    n = len(scored_articles)

    # Signal 1: Sentiment average (raw scores)
    avg_sent = sum(a["sentiment"] for a in scored_articles) / n

    # Signal 2: Volume spike (normalized by expected baseline)
    spike = min(5.0, max(1.0, n / 3.0))

    # Signal 3: Freshness-weighted confidence
    fw_scores = []
    for a in scored_articles:
        weight = _recency_weight(a.get("age_hours", 12.0))
        fw_scores.append(a["sentiment"] * weight)
    fw_conf = sum(fw_scores) / n if fw_scores else 0

    # Signal 4: Negative headline penalty
    neg_count = sum(1 for a in scored_articles if a["sentiment"] < -0.3)
    neg_penalty = -min(neg_count * 1.5, 6.0)

    # Combine into -15 to +15 range
    sent_score = round(avg_sent * 8.0, 2)        # ±8 max
    spike_bonus = min(5.0, round((spike - 1) * 3, 1)) if spike > 1.5 else 0
    fresh_bonus = round(fw_conf * 3.0, 2)         # ±3 freshness layer
    total_score = round(sent_score + spike_bonus + fresh_bonus + neg_penalty, 2)

    # Clamp to governance cap
    total_score = max(-15.0, min(15.0, total_score))

    # Build source breakdown
    source_breakdown = {}
    for a in scored_articles:
        src = a.get("source", "unknown")
        if src not in source_breakdown:
            source_breakdown[src] = {"count": 0, "avg_sentiment": 0.0}
        source_breakdown[src]["count"] += 1
        source_breakdown[src]["avg_sentiment"] += a["sentiment"]
    for src in source_breakdown:
        cnt = source_breakdown[src]["count"]
        source_breakdown[src]["avg_sentiment"] = round(
            source_breakdown[src]["avg_sentiment"] / cnt, 3
        )

    # FinBERT coverage metric
    finbert_count = sum(1 for a in scored_articles if a.get("method") == "finbert")
    finbert_coverage = round((finbert_count / n) * 100, 1) if n else 0.0

    # Build items list (legacy contract)
    items = [
        {
            "title": a["title"],
            "score": round(a["sentiment"], 3),
            "source": a.get("source", "unknown"),
            "age_h": a.get("age_hours", 12.0),
        }
        for a in sorted(scored_articles, key=lambda x: x.get("age_hours", 12.0))[:6]
    ]

    return {
        "score": total_score,
        "items": items,
        "source_breakdown": source_breakdown,
        "finbert_coverage": finbert_coverage,
        "method": "finbert" if finbert_coverage > 50 else "keyword",
        "article_count": n,
        "spike": round(spike, 2),
        "neg_penalty": round(neg_penalty, 1),
    }


# ──────────────────────────────────────────────────────────────
# Master Function: Normalize → Deduplicate → Score → Impact
# ──────────────────────────────────────────────────────────────

def process_articles(articles: list) -> dict:
    """
    Universal entry point. Accepts raw articles from ANY provider.
    
    Pipeline: Normalize → Deduplicate → FinBERT Score → News Impact
    
    Returns legacy-compatible dict: {score, items, source_breakdown, ...}
    """
    normalized = normalize_articles(articles)
    deduped = deduplicate_articles(normalized)
    scored = score_articles(deduped)
    return compute_news_impact(scored)
