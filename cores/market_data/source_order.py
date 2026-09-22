"""Which market-data providers the morning batch is allowed to call.

``krx`` is the KRX Data Marketplace scraper behind Kakao/Playwright login.
That session cannot be approved unattended, so the name is never constructed.
KRX Open API is previous-session end-of-day data and is not a substitute for
the 09:30 intraday snapshot; see docs/MARKET_DATA_NO_KAKAO_LOGIN.md.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ALLOWED = ("kis", "fdr", "naver")
DISABLED = frozenset({"krx"})
DEFAULT_ORDER = "kis,fdr,naver"


def sanitize_source_order(order: str | None) -> str:
    """Drop Kakao/Playwright KRX and unknown names. Empty input uses the default."""
    parts: list[str] = []
    for part in (order or "").split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name in DISABLED:
            logger.warning(
                "Skipping market data source %r: Kakao/Playwright KRX login is disabled",
                name,
            )
            continue
        if name not in ALLOWED:
            logger.warning("unknown market data source %r; skipping", name)
            continue
        if name not in parts:
            parts.append(name)
    if parts:
        return ",".join(parts)
    logger.warning(
        "no usable market data sources in %r; using %s", order, DEFAULT_ORDER
    )
    return DEFAULT_ORDER
