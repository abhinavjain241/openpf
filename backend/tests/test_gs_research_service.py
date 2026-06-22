"""Tests for the GS Marquee research bridge (filesystem-only, degradable)."""

import json
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.services import gs_research_service as gs


def _seed(dir_: Path) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "daily_brief_2026-02-11.md").write_text(
        "# Brief\nOld brief.\n", encoding="utf-8"
    )
    (dir_ / "daily_brief_2026-06-15.md").write_text(
        "# Brief\nLatest brief.\n", encoding="utf-8"
    )
    analyses = [
        {
            "_article_title": "GS Basics",
            "trade_ideas": [
                {
                    "instrument": "INDA",
                    "direction": "long",
                    "rationale": "India inflows",
                    "target": None,
                    "stop": None,
                }
            ],
            "key_views": [
                {
                    "asset_or_theme": "Indian Equities",
                    "direction": "bullish",
                    "conviction": "medium",
                    "summary": "Momentum",
                }
            ],
            "risk_flags": ["AI disintermediation"],
            "tickers_mentioned": ["INDA", "SPX"],
        },
        {"_article_title": "Failed one", "_error": "boom"},
    ]
    (dir_ / "analyses_2026-06-15.json").write_text(
        json.dumps(analyses), encoding="utf-8"
    )


def test_missing_dir_degrades(monkeypatch, tmp_path):
    monkeypatch.setenv("GS_RESEARCH_DIR", str(tmp_path / "nope"))
    assert gs.status()["available"] is False
    assert gs.get_brief()["available"] is False
    assert gs.get_trade_ideas()["available"] is False
    assert gs.list_briefs() == []


def test_latest_brief_and_status(monkeypatch, tmp_path):
    out = tmp_path / "marquee_output"
    _seed(out)
    monkeypatch.setenv("GS_RESEARCH_DIR", str(out))

    st = gs.status()
    assert st["available"] is True
    assert st["brief_count"] == 2
    assert st["latest_date"] == "2026-06-15"

    brief = gs.get_brief()
    assert brief["available"] is True
    assert brief["date"] == "2026-06-15"
    assert "Latest brief" in brief["markdown"]

    # explicit older date
    old = gs.get_brief("2026-02-11")
    assert old["available"] is True and "Old brief" in old["markdown"]

    # unknown date degrades with available_dates hint
    miss = gs.get_brief("1999-01-01")
    assert miss["available"] is False
    assert "2026-06-15" in miss["available_dates"]


def test_trade_ideas_flatten(monkeypatch, tmp_path):
    out = tmp_path / "marquee_output"
    _seed(out)
    monkeypatch.setenv("GS_RESEARCH_DIR", str(out))

    ideas = gs.get_trade_ideas()
    assert ideas["available"] is True
    assert ideas["date"] == "2026-06-15"
    assert ideas["article_count"] == 2  # includes the failed one
    assert any(i["instrument"] == "INDA" for i in ideas["trade_ideas"])
    assert ideas["trade_ideas"][0]["source"] == "GS Basics"
    assert "INDA" in ideas["tickers_mentioned"]
    assert "AI disintermediation" in ideas["risk_flags"]


def test_briefs_sorted_newest_first(monkeypatch, tmp_path):
    out = tmp_path / "marquee_output"
    _seed(out)
    monkeypatch.setenv("GS_RESEARCH_DIR", str(out))
    briefs = gs.list_briefs()
    assert [b["date"] for b in briefs] == ["2026-06-15", "2026-02-11"]


def test_compliance_rules(monkeypatch, tmp_path):
    out = tmp_path / "marquee_output"
    _seed(out)
    (tmp_path / "portfolio_config.yaml").write_text(
        "tradeable_universe:\n"
        "  no_preclearance:\n"
        "    broad_etfs_50plus_constituents: true\n"
        "  requires_preclearance:\n"
        "    single_stocks: true\n"
        "  notes:\n"
        "    - No trading in GS stock\n"
        "preferences:\n"
        "  risk_tolerance: moderate\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GS_RESEARCH_DIR", str(out))
    monkeypatch.delenv("GS_PORTFOLIO_CONFIG", raising=False)

    rules = gs.get_compliance_rules()
    assert rules["available"] is True
    assert rules["no_preclearance"]["broad_etfs_50plus_constituents"] is True
    assert rules["requires_preclearance"]["single_stocks"] is True
    assert rules["preferences"]["risk_tolerance"] == "moderate"
    assert gs.status()["compliance_config_present"] is True


_UNIVERSE_CSV = (
    "ticker,isin,type,short_name,full_name,currency,preclearance,underlier,parsekey,figi,group,status_reason\n"
    "INDA_US_EQ,US4642875565,ETF,INDA,iShares MSCI India ETF,USD,no,,,,group1,broad:120h:Equity\n"
    "SLB_US_EQ,AN8068571086,STOCK,SLB,SLB,USD,yes,,,,group3,single_security:SLB US\n"
    "ARKK_US_EQ,US00214Q1040,ETF,ARKK,ARK Innovation ETF,USD,review,,,,group2,narrow:35h\n"
)


def test_check_preclearance(monkeypatch, tmp_path):
    out = tmp_path / "marquee_output"
    out.mkdir(parents=True, exist_ok=True)
    (tmp_path / "isa_universe.csv").write_text(_UNIVERSE_CSV, encoding="utf-8")
    monkeypatch.setenv("GS_RESEARCH_DIR", str(out))
    monkeypatch.delenv("GS_UNIVERSE_CSV", raising=False)

    res = gs.check_preclearance(["INDA", "SLB_US_EQ", "ARKK", "ZZZZ"])
    assert res["available"] is True
    r = res["results"]

    # no-preclearance ETF resolves by short_name
    assert r["INDA"]["verdict"] == "no"
    assert r["INDA"]["preclearance_required"] is False

    # single stock resolves by full T212 ticker, gated
    assert r["SLB_US_EQ"]["verdict"] == "yes"
    assert r["SLB_US_EQ"]["preclearance_required"] is True

    # review is treated as gated
    assert r["ARKK"]["verdict"] == "review"
    assert r["ARKK"]["preclearance_required"] is True

    # unknown name fails safe (gated)
    assert r["ZZZZ"]["verdict"] == "unknown"
    assert r["ZZZZ"]["preclearance_required"] is True

    # surfaced in status + verdict rollup
    assert gs.status()["preclearance_universe_present"] is True
    counts = gs._universe_verdict_counts()
    assert counts.get("available") is True
    assert counts["by_verdict"]["no"] == 1
    assert counts["total"] == 3


def test_check_preclearance_missing_universe(monkeypatch, tmp_path):
    out = tmp_path / "marquee_output"
    out.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GS_RESEARCH_DIR", str(out))
    monkeypatch.delenv("GS_UNIVERSE_CSV", raising=False)
    res = gs.check_preclearance("SPY")
    assert res["available"] is False
