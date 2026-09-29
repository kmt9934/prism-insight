"""Broker residual sells: the strategy ledger closed a KR position but the
broker SELL was rejected, so shares are still held at the broker.

Typical cause: an exit decided after 15:30 on a KIS demo account, which accepts
neither after-hours closing orders nor reserved orders. The ledger stays as the
strategy decided; this module only finds the leftover broker quantity so the
next regular session can place the missing SELL.
"""

from __future__ import annotations

import datetime
import sqlite3
from typing import Any

_RETRY_PREFIX = "residual-retry"
_LIVE_OR_PENDING = ("SUBMITTED", "QUEUED", "UNKNOWN", "SUBMITTING", "CREATED")


def retry_decision_id(failed_intent_id: str, kst_date: str) -> str:
    """One retry identity per failed intent per KST day (idempotency key source)."""
    return f"{_RETRY_PREFIX}:{failed_intent_id}:{kst_date}"


def find_residual_sell_candidates(
    conn: sqlite3.Connection,
    *,
    now_utc: datetime.datetime,
    lookback_days: int = 10,
    max_attempts: int = 3,
    holdings_table: str = "stock_holdings",
) -> list[dict[str, Any]]:
    """Return FAILED KR SELL intents whose ledger position is closed.

    Skipped: the ledger holds the ticker again for that account, a later SELL
    for the same account/symbol was submitted or is in flight, or the retry
    budget for that failed intent is used up.
    """
    since = (now_utc - datetime.timedelta(days=lookback_days)).isoformat()
    try:
        failed = conn.execute(
            "SELECT id, account_id, symbol, quantity, created_at FROM order_intents "
            "WHERE market='KR' AND side='SELL' AND status='FAILED' "
            "AND (source_decision_id IS NULL OR source_decision_id NOT LIKE ?) "
            "AND created_at >= ? ORDER BY created_at",
            (f"{_RETRY_PREFIX}:%", since),
        ).fetchall()
    except sqlite3.Error:
        return []

    placeholders = ",".join("?" for _ in _LIVE_OR_PENDING)
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for intent_id, account_id, symbol, quantity, created_at in failed:
        key = (str(account_id), str(symbol))
        if key in seen:
            continue
        try:
            if conn.execute(
                f"SELECT 1 FROM {holdings_table} WHERE ticker=? AND account_key=? LIMIT 1",
                (symbol, account_id),
            ).fetchone():
                continue
            if conn.execute(
                "SELECT 1 FROM order_intents WHERE market='KR' AND side='SELL' "
                "AND account_id=? AND symbol=? AND created_at > ? "
                f"AND status IN ({placeholders}) LIMIT 1",
                (account_id, symbol, created_at, *_LIVE_OR_PENDING),
            ).fetchone():
                continue
            attempts = conn.execute(
                "SELECT COUNT(*) FROM order_intents WHERE source_decision_id LIKE ?",
                (f"{_RETRY_PREFIX}:{intent_id}:%",),
            ).fetchone()[0]
            if attempts >= max_attempts:
                continue
            name_row = conn.execute(
                "SELECT account_name FROM trading_history WHERE ticker=? AND account_key=? "
                "ORDER BY sell_date DESC LIMIT 1",
                (symbol, account_id),
            ).fetchone()
        except sqlite3.Error:
            continue
        if not name_row or not name_row[0]:
            continue
        seen.add(key)
        candidates.append(
            {
                "failed_intent_id": intent_id,
                "account_key": account_id,
                "account_name": name_row[0],
                "symbol": symbol,
                "quantity": int(quantity) if quantity else None,
            }
        )
    return candidates


def resolve_retry_quantity(intended: int | None, broker_qty: int) -> int:
    """Never sell more than the failed order intended or the broker holds."""
    broker_qty = max(int(broker_qty or 0), 0)
    if intended is None:
        return broker_qty
    return min(max(int(intended), 0), broker_qty)
