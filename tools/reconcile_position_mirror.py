#!/usr/bin/env python3
"""Close KR position-mirror rows whose legacy holding no longer exists.

The ``positions`` table mirrors ``stock_holdings``. While the shadow ledger was
switched off, sells removed holdings but left their mirror rows OPEN, so tools
reading ``positions`` saw positions the broker no longer held. This one-shot
tool finds OPEN KR rows without a matching holding and closes them, using the
recorded sell (``trading_history``) or the filled hardstop order
(``loop_a_inflight_orders``) for the exit time and price.

Dry run by default. ``--apply`` first writes a SQLite backup into
``--backup-dir`` and then updates only rows that are still OPEN. It never
inserts trading history; rows without a recorded sell are reported.

    python3 tools/reconcile_position_mirror.py --db stock_tracking_db.sqlite
    python3 tools/reconcile_position_mirror.py --db stock_tracking_db.sqlite \
        --apply --backup-dir /home/user/prism_backups
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
EXIT_KIND = "reconciled"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kst_text(iso_utc: str) -> str | None:
    try:
        stamp = datetime.fromisoformat(str(iso_utc).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def find_orphans(conn: sqlite3.Connection) -> list[dict]:
    """OPEN KR mirror rows with no stock_holdings row, plus exit evidence."""
    if not _table_exists(conn, "positions"):
        return []
    rows = conn.execute(
        """
        SELECT p.id, p.legacy_holding_id, p.account_id, p.symbol, p.opened_at, p.entry_price
        FROM positions p
        WHERE p.market='KR' AND p.status='OPEN'
          AND NOT EXISTS (
              SELECT 1 FROM stock_holdings h
              WHERE CAST(h.id AS TEXT)=p.legacy_holding_id AND h.account_key=p.account_id
          )
        ORDER BY p.id
        """
    ).fetchall()
    has_history = _table_exists(conn, "trading_history")
    has_loop_a = _table_exists(conn, "loop_a_inflight_orders")
    found = []
    for position_id, legacy_id, account_id, symbol, opened_at, entry_price in rows:
        item = {
            "position_id": position_id,
            "legacy_holding_id": legacy_id,
            "symbol": symbol,
            "opened_at": opened_at,
            "closed_at": None,
            "exit_price": None,
            "realized_pnl_pct": None,
            "evidence": "none",
        }
        if has_history:
            sale = conn.execute(
                """
                SELECT id, sell_date, sell_price, profit_rate FROM trading_history
                WHERE ticker=? AND account_key=? AND sell_date >= ?
                ORDER BY sell_date ASC LIMIT 1
                """,
                (symbol, account_id, opened_at or ""),
            ).fetchone()
            if sale:
                item.update(closed_at=sale[1], exit_price=sale[2], realized_pnl_pct=sale[3],
                            evidence=f"trading_history#{sale[0]}")
        if item["closed_at"] is None and has_loop_a:
            for order_id, submitted, order_no in conn.execute(
                """
                SELECT id, submitted_ts, order_no FROM loop_a_inflight_orders
                WHERE ticker=? AND market='KR' AND side='SELL' AND status='FILLED'
                ORDER BY submitted_ts ASC
                """,
                (symbol,),
            ):
                local = _kst_text(submitted)
                if local and local >= (opened_at or ""):
                    item.update(closed_at=local, evidence=f"loop_a_inflight_orders#{order_id} order {order_no}")
                    break
        found.append(item)
    return found


def backup_db(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{db_path.name}.bak-{stamp}-reconcile-positions"
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def apply(conn: sqlite3.Connection, orphans: list[dict]) -> int:
    now = _utc_now()
    closed = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in orphans:
            closed += conn.execute(
                """
                UPDATE positions
                SET status='CLOSED', closed_at=?, exit_price=COALESCE(?, exit_price),
                    realized_pnl_pct=COALESCE(?, realized_pnl_pct),
                    exit_kind=COALESCE(exit_kind, ?), updated_at=?
                WHERE id=? AND market='KR' AND status='OPEN'
                """,
                (item["closed_at"] or now, item["exit_price"], item["realized_pnl_pct"],
                 EXIT_KIND, now, item["position_id"]),
            ).rowcount
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return closed


def main(argv: list[str] | None = None) -> int:
    try:
        from prism_core.log_redaction import install_log_redaction

        install_log_redaction()
    except Exception:  # noqa: BLE001 - standalone use outside the repo root
        pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="stock_tracking_db.sqlite")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default=str(Path.home() / "prism_backups"))
    args = parser.parse_args(argv)
    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    try:
        orphans = find_orphans(conn)
        for item in orphans:
            print(json.dumps(item, ensure_ascii=False))
        print(f"orphan OPEN KR mirror rows: {len(orphans)}")
        missing = [i["position_id"] for i in orphans if not i["evidence"].startswith("trading_history")]
        if missing:
            print(f"no trading_history sell after opened_at (reported, not inserted): {missing}")
        if not args.apply or not orphans:
            print("dry run" if not args.apply else "nothing to apply")
            return 0
        backup = backup_db(db_path, Path(args.backup_dir))
        print(f"backup: {backup}")
        print(f"closed: {apply(conn, orphans)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
