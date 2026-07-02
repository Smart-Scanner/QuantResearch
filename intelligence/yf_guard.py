"""
intelligence/yf_guard.py — yfinance Circuit Breaker (DEPRECATED)

yfinance has been completely removed. All data now comes from:
  - Dhan.co: PE, PB, ROE, ROCE, EPS, Market Cap, Revenue, FCF, NPM
  - Angel One: Historical OHLCV, live feed
  - Fyers: Historical OHLCV (backup provider)

This module is kept as a stub for backward compatibility.
All functions are no-ops that return safe defaults.
"""

import logging

log = logging.getLogger("screener")


def yf_is_available() -> bool:
    """Always returns False — yfinance is removed."""
    return False


def yf_record_failure(source: str = "unknown") -> None:
    """No-op — yfinance is removed."""
    pass


def yf_record_success() -> None:
    """No-op — yfinance is removed."""
    pass


def yf_reset() -> None:
    """No-op — yfinance is removed."""
    pass


def yf_status() -> dict:
    """Return static status indicating yfinance is removed."""
    return {
        "yf_available": False,
        "yf_failure_count": 0,
        "yf_cooldown_remaining_s": 0,
        "yf_circuit_open": True,
        "yf_removed": True,
        "data_source": "dhan_angel_fyers",
    }


class YFinanceCircuitOpenError(RuntimeError):
    """Kept for backward compatibility."""
    pass


def get_yf_session():
    """No-op — yfinance is removed."""
    return None


def get_yf_ticker(symbol: str, source: str = "unknown"):
    """Always raises — yfinance is removed."""
    raise YFinanceCircuitOpenError(
        f"yfinance removed. Use Dhan/Angel/Fyers instead. (symbol={symbol}, source={source})"
    )


def get_yf_download(tickers, source: str = "unknown", **kwargs):
    """Always raises — yfinance is removed."""
    raise YFinanceCircuitOpenError(
        f"yfinance removed. Use Dhan/Angel/Fyers instead. (tickers={tickers}, source={source})"
    )
