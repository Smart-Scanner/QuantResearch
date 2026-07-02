"""
quantresearch/scoring_v1/asm_gsm.py — NSE surveillance status (ASM stage-aware + GSM).
================================================================================
ADDITIVE module for the scoring_v1 upstream QUALITY gate. Fetches the FREE NSE
surveillance lists and exposes a STAGE-AWARE rejection rule (revised spec):

  ASM (Additional Surveillance Measure):
      Stage 1 -> ALLOW   Stage 2 -> ALLOW   Stage 3 -> REJECT   Stage 4 -> REJECT
  GSM (Graded Surveillance Measure, ANY stage) -> REJECT
  Suspended -> REJECT        Delisted -> REJECT

MISSING NEVER EXCLUDES: if the fetch fails, or a symbol's ASM stage cannot be
determined, or suspended/delisted status is unknown, the symbol is NOT excluded
(treated as not-tripped) and the unknown is logged. We never fabricate status.

Public API:
  - get_surveillance(force_refresh=False) -> (asm_stage_map, gsm_set, status_dict)
        asm_stage_map: {symbol -> int stage 1..4 or None if listed-but-stage-unknown}
  - should_reject(symbol) -> (bool, reason|None)   # the gate's decision + reason tag
  - asm_stage(symbol) -> int|None ;  is_gsm(symbol) -> bool
  - fetch_status() -> dict
  - is_under_surveillance(symbol) -> bool  # legacy/back-compat (ASM-listed OR GSM)
"""
from __future__ import annotations

import io
import csv
import json
import time
import re
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("screener")

_CACHE_DIR = Path(__file__).resolve().parents[2] / "cache"
_CACHE_FILE = _CACHE_DIR / "asm_gsm_surveillance.json"

_NSE_HOME = "https://www.nseindia.com/"
_ASM_URL = "https://www.nseindia.com/api/reportASM"
_GSM_URL = "https://www.nseindia.com/api/reportGSM"

# Stages on the ASM Long-Term ladder that trigger exclusion.
# POLICY (2026-06-30): ASM fully ALLOWED — no ASM stage rejects (empty tuple).
# Only GSM (any stage) still rejects (handled in should_reject). To restore the
# prior "reject ASM stage 3/4" policy, set this back to (3, 4).
_ASM_REJECT_STAGES = ()

ASM_STAGES: dict = {}        # symbol -> stage int (1..4) or None
GSM_SYMBOLS: frozenset = frozenset()
_LAST_STATUS: dict = {"fetched": False, "source": None, "asm_count": 0, "gsm_count": 0}

_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}


def _nse_headers() -> dict:
    return {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _parse_stage(val) -> int | None:
    """Best-effort parse of an ASM stage from a NSE field value -> 1..4 or None."""
    if val is None:
        return None
    s = str(val).strip().upper()
    if not s:
        return None
    m = re.search(r"\b([1-4])\b", s)            # arabic '... 3 ...'
    if m:
        return int(m.group(1))
    m = re.search(r"\b(IV|III|II|I)\b", s)       # roman
    if m:
        return _ROMAN.get(m.group(1))
    return None


def _extract_asm_stage_map(payload) -> dict:
    """Walk an ASM payload -> {symbol: max stage found (int) or None}.

    Payload shapes vary; we collect any node carrying a 'symbol' plus any
    stage-like sibling field. If a symbol appears with no parseable stage, it maps
    to None (listed but stage-unknown -> ALLOW per missing-never-excludes).
    """
    out: dict = {}
    _stage_keys = ("stage", "asmSurvIndicator", "asm_stage", "survIndicator",
                   "surv_indicator", "longterm", "shortterm", "asmStage")

    def _node_stage(node: dict):
        for k, v in node.items():
            if k.lower() in (sk.lower() for sk in _stage_keys):
                st = _parse_stage(v)
                if st:
                    return st
        return None

    def _walk(node):
        if isinstance(node, dict):
            sym = node.get("symbol") or node.get("Symbol") or node.get("SYMBOL")
            if isinstance(sym, str) and sym.strip():
                s = sym.strip().upper()
                st = _node_stage(node)
                prev = out.get(s)
                # keep the MAX confirmed stage; None only if never seen a stage
                out[s] = max([x for x in (prev, st) if x], default=None)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for it in node:
                _walk(it)

    _walk(payload)
    return out


def _extract_symbols(payload) -> set:
    out: set = set()

    def _walk(node):
        if isinstance(node, dict):
            sym = node.get("symbol") or node.get("Symbol") or node.get("SYMBOL")
            if isinstance(sym, str) and sym.strip():
                out.add(sym.strip().upper())
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for it in node:
                _walk(it)

    _walk(payload)
    return out


def _read_cache() -> dict | None:
    try:
        if not _CACHE_FILE.exists():
            return None
        payload = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if payload.get("date") == _today_str() and "asm_stages" in payload:
            return payload
    except Exception as exc:
        log.debug("[ASM/GSM] cache read failed: %s", exc)
    return None


def _write_cache(asm_stages: dict, gsm: set, source: str) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({
            "date": _today_str(),
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "asm_stages": asm_stages,
            "gsm": sorted(gsm),
        }), encoding="utf-8")
    except Exception as exc:
        log.debug("[ASM/GSM] cache write failed: %s", exc)


def _fetch() -> tuple[dict, set, str]:
    """Fetch ASM stage map + GSM set. On ANY failure -> ({}, set(), 'fetch_failed')."""
    try:
        import requests
    except Exception as exc:
        log.warning("[ASM/GSM] requests unavailable: %s -> nobody excluded", exc)
        return {}, set(), "fetch_failed"

    session = requests.Session()
    try:
        session.get(_NSE_HOME, headers=_nse_headers(), timeout=10)
        time.sleep(1)
    except Exception as exc:
        log.debug("[ASM/GSM] homepage warm-up failed: %s", exc)

    asm_stages: dict = {}
    gsm: set = set()
    any_ok = False
    try:
        resp = session.get(_ASM_URL, headers=_nse_headers(), timeout=20)
        if resp.status_code == 200:
            try:
                asm_stages = _extract_asm_stage_map(resp.json())
            except Exception:
                asm_stages = {s: None for s in _extract_symbols_from_csv(resp.text)}
            if asm_stages:
                any_ok = True
                log.info("[ASM/GSM] ASM: %d symbols (stage-aware)", len(asm_stages))
        else:
            log.warning("[ASM/GSM] ASM endpoint returned %d", resp.status_code)
    except Exception as exc:
        log.warning("[ASM/GSM] ASM fetch failed: %s", exc)
    try:
        resp = session.get(_GSM_URL, headers=_nse_headers(), timeout=20)
        if resp.status_code == 200:
            try:
                gsm = _extract_symbols(resp.json())
            except Exception:
                gsm = _extract_symbols_from_csv(resp.text)
            if gsm:
                any_ok = True
                log.info("[ASM/GSM] GSM: %d symbols", len(gsm))
        else:
            log.warning("[ASM/GSM] GSM endpoint returned %d", resp.status_code)
    except Exception as exc:
        log.warning("[ASM/GSM] GSM fetch failed: %s", exc)

    if not any_ok:
        log.warning("[ASM/GSM] BOTH endpoints failed/empty -> nobody excluded (MISSING -> ALLOW)")
        return {}, set(), "fetch_failed"
    return asm_stages, gsm, "nse_api"


def _extract_symbols_from_csv(text: str) -> set:
    out: set = set()
    try:
        for row in csv.DictReader(io.StringIO(text)):
            for key in ("Symbol", "symbol", "SYMBOL", "Security Symbol"):
                if row.get(key) and row[key].strip():
                    out.add(row[key].strip().upper())
                    break
    except Exception:
        pass
    return out


def get_surveillance(force_refresh: bool = False):
    """Return (asm_stage_map, gsm_set, status). Cache-or-fetch; never raises."""
    global ASM_STAGES, GSM_SYMBOLS, _LAST_STATUS
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            ASM_STAGES = dict(cached.get("asm_stages", {}))
            GSM_SYMBOLS = frozenset(cached.get("gsm", []))
            _LAST_STATUS = {"fetched": True, "source": f"cache({cached.get('source','?')})",
                            "asm_count": len(ASM_STAGES), "gsm_count": len(GSM_SYMBOLS),
                            "fetched_at": cached.get("fetched_at")}
            return dict(ASM_STAGES), set(GSM_SYMBOLS), dict(_LAST_STATUS)
    asm_stages, gsm, source = _fetch()
    if source != "fetch_failed":
        _write_cache(asm_stages, gsm, source)
    ASM_STAGES = dict(asm_stages)
    GSM_SYMBOLS = frozenset(gsm)
    _LAST_STATUS = {"fetched": source != "fetch_failed", "source": source,
                    "asm_count": len(ASM_STAGES), "gsm_count": len(GSM_SYMBOLS),
                    "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    return dict(ASM_STAGES), set(GSM_SYMBOLS), dict(_LAST_STATUS)


def _ensure_loaded():
    if not ASM_STAGES and not GSM_SYMBOLS and not _LAST_STATUS.get("fetched"):
        get_surveillance()


def asm_stage(symbol: str):
    if not symbol:
        return None
    _ensure_loaded()
    return ASM_STAGES.get(symbol.strip().upper())


def is_gsm(symbol: str) -> bool:
    if not symbol:
        return False
    _ensure_loaded()
    return symbol.strip().upper() in GSM_SYMBOLS


def should_reject(symbol: str):
    """Apply the revised surveillance rule -> (reject: bool, reason: str|None).

    Reject on: ASM stage 3/4, GSM (any), suspended, delisted. ASM stage 1/2 or
    stage-unknown -> ALLOW. suspended/delisted are not in our pipeline yet ->
    treated as unknown -> ALLOW (missing never excludes). Never raises.
    """
    if not symbol:
        return False, None
    try:
        _ensure_loaded()
        s = symbol.strip().upper()
        if s in GSM_SYMBOLS:
            return True, "quality:gsm"
        st = ASM_STAGES.get(s)
        if st in _ASM_REJECT_STAGES:
            return True, f"quality:asm_stage{st}"
        # ASM stage 1/2 or unknown -> allow. suspended/delisted unknown -> allow.
        return False, None
    except Exception as exc:
        log.debug("[ASM/GSM] should_reject(%s) failed -> allow: %s", symbol, exc)
        return False, None


def is_under_surveillance(symbol: str) -> bool:
    """Back-compat: True iff ASM-listed (any stage) OR GSM-listed."""
    if not symbol:
        return False
    _ensure_loaded()
    s = symbol.strip().upper()
    return s in ASM_STAGES or s in GSM_SYMBOLS


def get_surveillance_sets(force_refresh: bool = False):
    """Back-compat shim: (asm_symbol_set, gsm_set, status)."""
    asm_stages, gsm, status = get_surveillance(force_refresh)
    return set(asm_stages.keys()), gsm, status


def fetch_status() -> dict:
    return dict(_LAST_STATUS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    a, g, st = get_surveillance(force_refresh=True)
    print("status:", st)
    print("ASM sample (sym->stage):", dict(sorted(a.items())[:10]))
    print("GSM sample:", sorted(g)[:10])
    print("should_reject sample:", {s: should_reject(s) for s in list(a)[:5]})
