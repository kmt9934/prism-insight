#!/usr/bin/env python3
"""Warm the KIS previous-session history cache before the KR screening runs.

The 09:30 morning batch needs one dated daily bar per listed stock. Cold, that
is ~2,700 sequential KIS calls, and on a mock (vps) app key they share a very
low per-second quota with the intraday sell loops, so the batch cannot finish
inside its deadline. Fetching the same exact bars earlier, while nothing else
uses the key, lets the morning and afternoon batches read the verified cache.
Read-only quotations; never places or changes orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import logging
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from check_market_day import is_market_day
from cores.kis_market_snapshot import (
    KisSnapshotError,
    _today_kst,
    fetch_kis_master_data,
    fetch_kis_previous_history,
    previous_session,
)

logger = logging.getLogger("prefetch_kr_previous_history")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=0.5,
                        help="seconds between KIS calls (mock keys allow very few per second)")
    parser.add_argument("--max-duration", type=float, default=5400)
    args = parser.parse_args()
    # check_market_day configures a file handler at import; cron needs stdout.
    logging.basicConfig(level=logging.INFO, force=True,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    trade_date = _today_kst()
    if not is_market_day(datetime.strptime(trade_date, "%Y%m%d").date()):
        logger.info("Not a market day (%s); nothing to prefetch", trade_date)
        return 0
    started = time.monotonic()
    try:
        master = fetch_kis_master_data()
        prev_date = previous_session(trade_date)
        # Zero-cap issues are excluded by the screening bundle and may have no bar.
        codes = sorted(code for code, cap in master.cap_df["시가총액"].items()
                       if code in master.names and cap > 0)
        logger.info("Prefetching %s bars for %d stocks (interval=%.2fs)", prev_date, len(codes), args.interval)
        result = fetch_kis_previous_history(
            codes, prev_date, request_interval_sec=args.interval,
            max_duration_sec=args.max_duration,
        )
    except KisSnapshotError as exc:
        # Rows fetched so far stay cached; the batch fetches only the remainder.
        logger.error("Prefetch incomplete after %.0fs: %s", time.monotonic() - started, exc)
        return 1
    logger.info("Prefetch complete: %d/%d rows for %s in %.0fs",
                len(result), len(codes), prev_date, time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
