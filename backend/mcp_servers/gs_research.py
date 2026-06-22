"""GS Marquee research MCP server — bridges the gs-summarize pipeline output.

Gives Archie read-only tools over the companion ``gs-summarize`` repo's daily
Goldman Sachs Marquee morning briefs, their structured trade ideas / key views,
and the user's own preclearance compliance rules (portfolio_config.yaml). Used so
Archie can FUSE GS desk research with the live portfolio. The directory is
resolved by app.services.gs_research_service (env GS_RESEARCH_DIR → ConfigStore →
default sibling repo); everything degrades gracefully when files are absent.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.services import gs_research_service as gs

# ── Logging (file-based — stdout is reserved for MCP protocol) ──
_LOG_DIR = Path(
    os.environ.get("MCP_LOG_DIR") or (Path(__file__).resolve().parent.parent / "logs")
)
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    import tempfile

    _LOG_DIR = Path(tempfile.gettempdir()) / "mypf-mcp-logs"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("gs-research-mcp")
logger.setLevel(logging.INFO)
logger.propagate = False
_fh = logging.FileHandler(_LOG_DIR / "gs_research.log")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
logger.addHandler(_fh)

mcp = FastMCP(
    "gs_research",
    instructions=(
        "Goldman Sachs Marquee research bridge. Tools: get_gs_research_status "
        "(is the feed present + how fresh), get_latest_gs_brief / get_gs_brief "
        "(the markdown morning brief), list_gs_briefs (available dates), "
        "get_gs_trade_ideas (structured trade ideas + key views + risk flags from "
        "the desk research), get_compliance_rules (the user's OWN preclearance "
        "constraints — what can be traded freely vs. what needs preclearance), and "
        "check_preclearance (authoritative per-instrument verdict for specific names "
        "from the ISA universe). "
        "Use these to FUSE GS desk research with the live portfolio: map ideas onto "
        "held names, flag risks to existing positions, and only surface ideas that "
        "are compliance-clean. Always check freshness (stale_days) and cite the brief "
        "date. This is research INPUT, never an executed trade."
    ),
)


def _fmt(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def get_gs_research_status() -> str:
    """Is the GS research feed available, how many briefs exist, and how fresh is
    the latest one (stale_days). Call this first if unsure the bridge is wired."""
    try:
        return _fmt({"ok": True, **gs.status()})
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_gs_research_status error: %s", exc)
        return _fmt({"ok": False, "error": str(exc)})


@mcp.tool()
def list_gs_briefs(limit: int = 30) -> str:
    """List available GS morning briefs (newest first) as {date, file}.

    Args:
        limit: max briefs to list (default 30).
    """
    try:
        return _fmt({"ok": True, "briefs": gs.list_briefs(limit=limit)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_gs_briefs error: %s", exc)
        return _fmt({"ok": False, "error": str(exc)})


@mcp.tool()
def get_latest_gs_brief() -> str:
    """The most recent GS Marquee morning brief (full markdown). Check the `date`
    field and treat anything more than ~1 day old as potentially stale."""
    try:
        return _fmt({"ok": True, **gs.get_brief(None)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_latest_gs_brief error: %s", exc)
        return _fmt({"ok": False, "error": str(exc)})


@mcp.tool()
def get_gs_brief(date: str) -> str:
    """A specific day's GS Marquee morning brief (full markdown).

    Args:
        date: brief date as YYYY-MM-DD.
    """
    try:
        return _fmt({"ok": True, **gs.get_brief(date)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_gs_brief error: %s", exc)
        return _fmt({"ok": False, "error": str(exc)})


@mcp.tool()
def get_gs_trade_ideas(date: str = "") -> str:
    """Structured trade ideas, key views, risk flags, and mentioned tickers from
    the desk research, flattened across articles. Each carries its source article.

    Args:
        date: brief date YYYY-MM-DD, or empty for the latest.
    """
    try:
        return _fmt({"ok": True, **gs.get_trade_ideas(date or None)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_gs_trade_ideas error: %s", exc)
        return _fmt({"ok": False, "error": str(exc)})


@mcp.tool()
def check_preclearance(symbols: str) -> str:
    """Authoritative per-instrument preclearance verdict from the ISA universe.

    Resolves each symbol against `isa_universe.csv` (the trade-automation
    classifier's real per-instrument verdict) — far more precise than the category
    rules in get_compliance_rules. Use this to confirm whether a SPECIFIC name Abhinav
    can trade freely before surfacing or queuing an idea.

    Verdicts: `no` = trade freely (no preclearance), `review` = needs review,
    `yes` = preclearance required, `unknown` = not in the universe (treat as gated).

    Args:
        symbols: one or more symbols/tickers, comma-separated. Accepts plain symbols
            (SPY), short names, ISINs, or full T212 codes (SLB_US_EQ).
    """
    try:
        items = [s.strip() for s in (symbols or "").split(",") if s.strip()]
        return _fmt({"ok": True, **gs.check_preclearance(items)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("check_preclearance error: %s", exc)
        return _fmt({"ok": False, "error": str(exc)})


@mcp.tool()
def get_compliance_rules() -> str:
    """The user's OWN trading-compliance rules (preclearance) from their
    portfolio_config.yaml: what can be traded freely (broad ETFs/index options with
    50+ constituents, commodities, crypto, FX, treasury ETFs) vs. what REQUIRES
    preclearance (single stocks, narrow sector ETFs, single-stock options), plus
    notes and preferences. Apply these to every idea you surface."""
    try:
        return _fmt({"ok": True, **gs.get_compliance_rules()})
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_compliance_rules error: %s", exc)
        return _fmt({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    mcp.run()
