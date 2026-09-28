"""Open DART (전자공시) fundamentals prefetch for KR company_status scoring.

Fetches structured quarterly/annual account lines so the company_status agent
does not have to rely solely on WiseReport scraping for F1–F4 checks.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://opendart.fss.or.kr/api"
_KST = ZoneInfo("Asia/Seoul")
_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "dart"
_CORP_MAP_FILE = _CACHE_DIR / "corp_code_map.json"
_CORP_MAP_MAX_AGE_SEC = 7 * 24 * 3600

# Open DART report codes (분기보고서 등)
_REPORT_CODES = (
    ("11014", "3분기"),
    ("11012", "반기"),
    ("11013", "1분기"),
    ("11011", "사업보고서"),
)


def _api_key() -> str:
    key = (os.getenv("DART_API_KEY") or os.getenv("OPENDART_API_KEY") or "").strip()
    if key:
        return key
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except Exception:
        pass
    return (os.getenv("DART_API_KEY") or os.getenv("OPENDART_API_KEY") or "").strip()


def _ensure_cache_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _download_corp_map(auth_key: str) -> dict[str, str]:
    """Return {6-digit stock_code: 8-digit corp_code}."""
    _ensure_cache_dir()
    url = f"{_BASE_URL}/corpCode.xml"
    resp = requests.get(url, params={"crtfc_key": auth_key}, timeout=60)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_name = [n for n in zf.namelist() if n.lower().endswith(".xml")][0]
        root = ET.fromstring(zf.read(xml_name))
    mapping: dict[str, str] = {}
    for item in root.findall("list"):
        stock = (item.findtext("stock_code") or "").strip()
        corp = (item.findtext("corp_code") or "").strip()
        if stock and corp and stock.isdigit():
            mapping[stock.zfill(6)] = corp
    _CORP_MAP_FILE.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping


def _load_corp_map(auth_key: str) -> dict[str, str]:
    if _CORP_MAP_FILE.is_file():
        age = time.time() - _CORP_MAP_FILE.stat().st_mtime
        if age < _CORP_MAP_MAX_AGE_SEC:
            try:
                return json.loads(_CORP_MAP_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
    return _download_corp_map(auth_key)


def stock_to_corp_code(stock_code: str, auth_key: str | None = None) -> str | None:
    key = auth_key or _api_key()
    if not key:
        return None
    code = str(stock_code).zfill(6)
    return _load_corp_map(key).get(code)


def _fetch_single_accounts(
    auth_key: str,
    corp_code: str,
    bsns_year: int,
    reprt_code: str,
    fs_div: str = "CFS",
) -> list[dict[str, Any]]:
    url = f"{_BASE_URL}/fnlttSinglAcnt.json"
    resp = requests.get(
        url,
        params={
            "crtfc_key": auth_key,
            "corp_code": corp_code,
            "bsns_year": str(bsns_year),
            "reprt_code": reprt_code,
            "fs_div": fs_div,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if str(payload.get("status")) != "000":
        msg = payload.get("message") or payload.get("status")
        raise RuntimeError(f"DART fnlttSinglAcnt {bsns_year}/{reprt_code}: {msg}")
    return payload.get("list") or []


def _parse_amount(raw: Any) -> float | None:
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    if not s or s in {"-", ""}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pick_account(rows: list[dict[str, Any]], patterns: tuple[str, ...]) -> float | None:
    for row in rows:
        name = (row.get("account_nm") or "").replace(" ", "")
        for pat in patterns:
            if pat in name:
                val = _parse_amount(row.get("thstrm_amount"))
                if val is not None:
                    return val
    return None


def _period_snapshot(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    revenue = _pick_account(rows, ("매출액", "수익매출", "영업수익"))
    op_profit = _pick_account(rows, ("영업이익", "영업이익손실"))
    net_income = _pick_account(rows, ("당기순이익", "분기순이익", "순이익"))
    total_debt = _pick_account(rows, ("부채총계",))
    total_equity = _pick_account(rows, ("자본총계", "자본합계"))
    roe = None
    if net_income is not None and total_equity and total_equity > 0:
        roe = (net_income / total_equity) * 100.0
    debt_ratio = None
    if total_debt is not None and total_equity and total_equity > 0:
        debt_ratio = (total_debt / total_equity) * 100.0
    return {
        "revenue": revenue,
        "operating_profit": op_profit,
        "net_income": net_income,
        "roe_pct": roe,
        "debt_ratio_pct": debt_ratio,
    }


def fetch_fundamental_periods(
    stock_code: str,
    reference_date: str | None = None,
    max_periods: int = 4,
) -> list[dict[str, Any]]:
    """Fetch recent DART financial periods for a stock code."""
    auth_key = _api_key()
    if not auth_key:
        return []
    corp_code = stock_to_corp_code(stock_code, auth_key)
    if not corp_code:
        logger.warning("DART corp_code not found for %s", stock_code)
        return []

    if reference_date:
        ref_year = int(reference_date[:4])
    else:
        ref_year = datetime.now(_KST).year

    periods: list[dict[str, Any]] = []
    for year in (ref_year, ref_year - 1):
        for reprt_code, label in _REPORT_CODES:
            try:
                rows = _fetch_single_accounts(auth_key, corp_code, year, reprt_code)
            except Exception as exc:
                logger.debug("DART skip %s %s %s: %s", stock_code, year, reprt_code, exc)
                continue
            if not rows:
                continue
            snap = _period_snapshot(rows)
            if snap["revenue"] is None and snap["operating_profit"] is None:
                continue
            periods.append(
                {
                    "year": year,
                    "report_code": reprt_code,
                    "report_label": label,
                    **snap,
                }
            )
            if len(periods) >= max_periods:
                return periods
    return periods


def format_fundamentals_markdown(
    company_name: str,
    stock_code: str,
    reference_date: str,
    periods: list[dict[str, Any]] | None = None,
) -> str:
    """Markdown block injected into company_status agent context."""
    if periods is None:
        periods = fetch_fundamental_periods(stock_code, reference_date)
    if not periods:
        return ""

    lines = [
        "## Open DART 공시 재무 (검증된 정형 데이터)",
        f"- 종목: {company_name} ({stock_code})",
        f"- 기준일: {reference_date}",
        "- 아래 수치는 금융감독원 전자공시 Open DART API 원문입니다. F1~F4 펀더멘털 게이트 판단 시 **우선 사용**하세요.",
        "",
        "| 연도 | 보고서 | 매출액 | 영업이익 | 당기순이익 | ROE(%) | 부채비율(%) |",
        "|------|--------|--------|----------|------------|--------|-------------|",
    ]
    for p in periods:
        def fmt(v: float | None) -> str:
            if v is None:
                return "N/A"
            if abs(v) >= 1e8:
                return f"{v/1e8:.1f}억"
            return f"{v:,.0f}"

        lines.append(
            "| {year} | {label} | {rev} | {op} | {ni} | {roe} | {debt} |".format(
                year=p["year"],
                label=p["report_label"],
                rev=fmt(p.get("revenue")),
                op=fmt(p.get("operating_profit")),
                ni=fmt(p.get("net_income")),
                roe=f"{p['roe_pct']:.1f}" if p.get("roe_pct") is not None else "N/A",
                debt=f"{p['debt_ratio_pct']:.1f}" if p.get("debt_ratio_pct") is not None else "N/A",
            )
        )
    lines.append("")
    lines.append(
        "최근 2개 분기 영업이익·매출이 위 표에 있으면 '자료 없음'으로 처리하지 말고 해당 수치를 인용하세요."
    )
    return "\n".join(lines)


def prefetch_dart_fundamentals_markdown(
    company_name: str,
    company_code: str,
    reference_date: str,
) -> str:
    if not _api_key():
        return ""
    try:
        md = format_fundamentals_markdown(company_name, company_code, reference_date)
        if md:
            logger.info("Prefetched DART fundamentals for %s(%s)", company_name, company_code)
        return md
    except Exception as exc:
        logger.warning("DART prefetch failed for %s: %s", company_code, exc)
        return ""
