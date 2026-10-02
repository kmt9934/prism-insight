"""Observed 2026-10-02: one base-price-restated master volume halted all KR screening."""
import pandas as pd
import pytest

from cores import kis_market_snapshot as m

TODAY, PREV = "20261002", "20261001"
SAMSUNG_BIO = {"Open": 1390000, "High": 1441000, "Low": 1389000, "Close": 1429000,
               "Volume": 55677, "Amount": 79303452500}
NORMAL = {"Open": 10, "High": 12, "Low": 9, "Close": 11, "Volume": 1000, "Amount": 11000}


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setattr(m, "_today_kst", lambda: TODAY)
    monkeypatch.setattr(m, "previous_session", lambda date: PREV)

    def build(rows, master_volumes, base_prices, flags=None):
        codes = list(rows)
        previous = pd.DataFrame([rows[c] for c in codes], index=codes, dtype=float)
        cached = previous.copy(deep=True)
        cap = pd.DataFrame({"시가총액": [1e12] * len(codes)}, index=codes)
        master = m.KisMasterData(
            {c: c for c in codes}, cap, {}, TODAY, {}, {}, master_volumes, base_prices,
            flags or dict.fromkeys(codes, ("00", "00", "00")),
        )
        fetched = {}

        def snapshot(requested):
            fetched["codes"] = list(requested)
            return previous.loc[list(requested)].copy()

        result = m.build_kis_snapshot_bundle(
            TODAY, master_fetcher=lambda: master, history_fetcher=lambda *_: previous,
            snapshot_fetcher=snapshot,
        )
        return result, cached, previous, fetched

    return build


def test_observed_restatement_excludes_only_that_issue(case):
    rows = {"005930": NORMAL, "207940": SAMSUNG_BIO}
    result, cached, previous, fetched = case(
        rows, {"005930": 1000, "207940": 55248}, {"005930": "11", "207940": "001418000"},
        flags={"005930": ("00", "00", "00"), "207940": ("01", "00", "01")},
    )
    assert list(result.snapshot.index) == ["005930"]
    assert list(result.prev_snapshot.index) == ["005930"]
    assert list(result.cap_df.index) == ["005930"]
    assert fetched["codes"] == ["005930"]
    assert result.snapshot.attrs["price_adjusted_excluded"] == ["207940"]
    assert result.cap_df.attrs["master_price_adjusted_excluded"] == ["207940"]
    pd.testing.assert_frame_equal(previous, cached)  # cache-aware history object untouched


def test_unexplained_mismatch_still_halts(case):
    rows = {"005930": NORMAL, "207940": SAMSUNG_BIO}
    with pytest.raises(m.KisSnapshotError, match="volume/session mismatch"):
        case(rows, {"005930": 1000, "207940": 55000}, {"005930": "11", "207940": "001418000"})


def test_mismatch_without_base_price_change_halts(case):
    rows = {"005930": NORMAL, "207940": SAMSUNG_BIO}
    with pytest.raises(m.KisSnapshotError, match="volume/session mismatch"):
        case(rows, {"005930": 1000, "207940": 55248}, {"005930": "11", "207940": "001429000"})


def test_one_unexplained_mismatch_blocks_even_with_explained_ones(case):
    rows = {"005930": NORMAL, "207940": SAMSUNG_BIO, "000660": NORMAL}
    with pytest.raises(m.KisSnapshotError, match="volume/session mismatch"):
        case(rows, {"005930": 1000, "207940": 55248, "000660": 999},
             {"005930": "11", "207940": "001418000", "000660": "11"})


def test_more_than_limit_restatements_halt(case):
    rows = {f"{i:06d}": SAMSUNG_BIO for i in range(1, 12)}
    rows["005930"] = NORMAL
    volumes = {c: 55248 for c in rows}
    volumes["005930"] = 1000
    bases = {c: "001418000" for c in rows}
    bases["005930"] = "11"
    with pytest.raises(m.KisSnapshotError, match="volume/session mismatch"):
        case(rows, volumes, bases)


def test_whole_universe_restated_halts(case):
    with pytest.raises(m.KisSnapshotError, match="volume/session mismatch"):
        case({"207940": SAMSUNG_BIO}, {"207940": 55248}, {"207940": "001418000"})


@pytest.mark.parametrize("master,daily,base,close,expected", [
    (55248, 55677, "001418000", 1429000, True),
    (55249, 55677, "001418000", 1429000, True),   # within one share of the exact ratio
    (55250, 55677, "001418000", 1429000, False),
    (55677, 55677, "001418000", 1429000, False),  # equal volumes are handled as exact elsewhere
    (55248, 55677, "001429000", 1429000, False),  # no base change, no restatement evidence
    (55248, 55677, "", 1429000, False),
    (55248, 55677, "x", 1429000, False),
    (55248, 0, "001418000", 1429000, False),
    (float("nan"), 55677, "001418000", 1429000, False),
])
def test_restatement_rule(master, daily, base, close, expected):
    assert m._price_adjusted_volume_matches(master, daily, base, close) is expected
