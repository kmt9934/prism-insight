"""Stale OPEN position-mirror rows are closed (tool + sell path with ledger off)."""

from __future__ import annotations

import sqlite3

import pytest

from tools import reconcile_position_mirror as tool

ACCT = "vps:50204184:01"


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE stock_holdings (id INTEGER PRIMARY KEY, account_key TEXT, ticker TEXT);
        CREATE TABLE trading_history (id INTEGER PRIMARY KEY, account_key TEXT, ticker TEXT,
            sell_date TEXT, sell_price REAL, profit_rate REAL);
        CREATE TABLE loop_a_inflight_orders (id INTEGER PRIMARY KEY, ticker TEXT, market TEXT,
            side TEXT, order_no TEXT, qty INTEGER, status TEXT, submitted_ts TEXT);
        CREATE TABLE positions (id TEXT PRIMARY KEY, market TEXT, legacy_holding_id TEXT,
            account_id TEXT, symbol TEXT, status TEXT, opened_at TEXT, closed_at TEXT,
            exit_intent_id TEXT, entry_price REAL, exit_price REAL, realized_pnl_pct REAL,
            exit_kind TEXT, updated_at TEXT);
        """
    )
    rows = [
        ("legacy:KR:2", "2", "061040", "2026-09-11 11:53:28"),
        ("legacy:KR:3", "3", "161890", "2026-09-14 12:18:55"),
        ("legacy:KR:5", "5", "058610", "2026-09-16 09:30:14"),
        ("legacy:KR:9", "9", "005930", "2026-10-01 09:10:00"),  # still held
    ]
    for pid, legacy, symbol, opened in rows:
        conn.execute(
            "INSERT INTO positions (id, market, legacy_holding_id, account_id, symbol, status, opened_at, entry_price)"
            " VALUES (?, 'KR', ?, ?, ?, 'OPEN', ?, 100)", (pid, legacy, ACCT, symbol, opened))
    conn.execute("INSERT INTO stock_holdings VALUES (9, ?, '005930')", (ACCT,))
    conn.execute("INSERT INTO trading_history VALUES (1, ?, '061040', '2026-09-29 09:07:12', 7200, -0.96)", (ACCT,))
    conn.execute("INSERT INTO trading_history VALUES (2, ?, '161890', '2026-09-23 15:59:19', 147000, -0.41)", (ACCT,))
    conn.execute("INSERT INTO trading_history VALUES (3, ?, '058610', '2026-09-16 09:28:12', 98700, -3.99)", (ACCT,))
    conn.execute("INSERT INTO loop_a_inflight_orders VALUES (2, '058610', 'KR', 'SELL', '0000008319', 4, 'FILLED',"
                 " '2026-09-16T00:31:20.768900+00:00')")
    conn.commit()
    return conn


def test_dry_run_finds_the_three_sold_rows_and_changes_nothing(tmp_path, capsys):
    path = tmp_path / "db.sqlite"
    _db(path).close()
    assert tool.main(["--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "orphan OPEN KR mirror rows: 3" in out and "dry run" in out
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0] == 4


def test_apply_backs_up_then_closes_with_sell_evidence(tmp_path, capsys):
    path = tmp_path / "db.sqlite"
    _db(path).close()
    backups = tmp_path / "backups"
    assert tool.main(["--db", str(path), "--apply", "--backup-dir", str(backups)]) == 0
    assert len(list(backups.iterdir())) == 1
    conn = sqlite3.connect(path)
    got = {r[0]: r[1:] for r in conn.execute("SELECT symbol, status, closed_at, exit_price, exit_kind FROM positions")}
    assert got["061040"] == ("CLOSED", "2026-09-29 09:07:12", 7200.0, "reconciled")
    assert got["161890"][:2] == ("CLOSED", "2026-09-23 15:59:19")
    # 058610 lot 5 was opened after the recorded sell; the filled hardstop order closes it.
    assert got["058610"][:2] == ("CLOSED", "2026-09-16 09:31:20")
    assert got["005930"][0] == "OPEN"
    assert conn.execute("SELECT COUNT(*) FROM trading_history").fetchone()[0] == 3  # nothing inserted
    # Re-running is a no-op.
    assert tool.main(["--db", str(path), "--apply", "--backup-dir", str(backups)]) == 0
    assert "orphan OPEN KR mirror rows: 0" in capsys.readouterr().out


def test_sell_path_closes_existing_open_mirror_when_ledger_disabled(tmp_path):
    pytest.importorskip("pandas")
    import stock_tracking_agent as sta
    from prism_core.positions import PositionStore

    conn = sqlite3.connect(tmp_path / "db.sqlite")
    store = PositionStore(conn.cursor())
    store.ensure_schema()
    conn.commit()
    store.open_legacy_position(
        market="KR", legacy_holding_id=7, account_id=ACCT, account_name="모의-메인",
        symbol="000660", entry_price=100.0, opened_at="2026-10-01 09:00:00",
    )
    conn.commit()
    agent = sta.StockTrackingAgent.__new__(sta.StockTrackingAgent)
    agent.conn, agent.cursor = conn, conn.cursor()
    agent.position_ledger_shadow_enabled = False
    assert agent._mirror_position_closed(
        legacy_holding_id=7, account_key=ACCT, exit_price=95.0, realized_pnl_pct=-5.0,
        exit_kind="stop", closed_at="2026-10-02 10:00:00",
    )
    conn.commit()
    status, closed_at = conn.execute("SELECT status, closed_at FROM positions WHERE legacy_holding_id='7'").fetchone()
    assert (status, closed_at) == ("CLOSED", "2026-10-02 10:00:00")
    # No row at all (ledger never ran): still a silent no-op.
    assert agent._mirror_position_closed(
        legacy_holding_id=8, account_key=ACCT, exit_price=1.0, realized_pnl_pct=0.0,
        exit_kind="stop", closed_at="2026-10-02 10:00:00",
    )
