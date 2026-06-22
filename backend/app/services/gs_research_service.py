"""GS Marquee research bridge — reads the gs-summarize pipeline output.

The data layer that lets Archie fuse Goldman Sachs Marquee desk research with the
live portfolio. The companion repo ``gs-summarize`` scrapes GS Marquee articles
and writes, per run, a markdown morning brief plus a structured analyses JSON to
its ``marquee_output/`` directory. It also holds the user's own hedge-fund
trading constraints in ``portfolio_config.yaml``. This service reads those files
read-only — gs-summarize stays the source of truth; nothing here ever writes back.

Filesystem-only by design (both repos live on the same machine). Keyless /
degradable: if the directory or files are missing, every function returns an
empty/``available: False`` payload rather than raising, so the fusion brief and
chat answers degrade gracefully instead of crashing.

Directory resolution (first hit wins):
  1. ``GS_RESEARCH_DIR`` env var (passed to MCP subprocesses)
  2. ConfigStore ``data_providers.gs_research_dir``
  3. Default sibling repo: ``<openpf repo>/../gs-summarize/marquee_output``

Stable contract: these functions are what the ``gs_research`` MCP tools and the
``gs_fusion_brief`` scheduled task both call, so the layout behind them can change
without touching agents or the loop.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BRIEF_GLOB = "daily_brief_*.md"
_ANALYSES_GLOB = "analyses_*.json"
_UNIVERSE_CSV = "isa_universe.csv"
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Verdict ranking for the compliance gate — most restrictive wins when a symbol
# resolves to several listings. "unknown" is treated as gated (fail-safe).
_VERDICT_RANK = {"no": 0, "review": 1, "yes": 2, "unknown": 3}

# Loaded universe cache, invalidated on file mtime change.
_universe_cache: dict[str, Any] = {"mtime": None, "path": None, "rows": [], "index": {}}


# ── Directory / path resolution ───────────────────────────────────────────────


def _default_dir() -> Path:
    """Default to the sibling ``gs-summarize`` checkout's output directory.

    ``project_root()`` is the openpf repo root; its parent is the dev tree that
    holds both repos side by side.
    """
    try:
        from app.services.claude_sdk_config import project_root

        return (project_root().parent / "gs-summarize" / "marquee_output").resolve()
    except Exception:  # noqa: BLE001
        return (
            Path(__file__).resolve().parents[3].parent
            / "gs-summarize"
            / "marquee_output"
        ).resolve()


def research_dir() -> Path:
    """Resolve the GS marquee output directory (env → ConfigStore → default)."""
    env = os.environ.get("GS_RESEARCH_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    try:
        from app.core.database import SessionLocal
        from app.services.config_store import ConfigStore

        with SessionLocal() as db:
            cfg = str(
                ConfigStore(db).get("data_providers", {}).get("gs_research_dir", "")
                or ""
            ).strip()
        if cfg:
            return Path(cfg).expanduser().resolve()
    except Exception:  # noqa: BLE001
        pass
    return _default_dir()


def _portfolio_config_path() -> Path:
    """The user's trading-constraints YAML lives at the gs-summarize repo root,
    i.e. the parent of the marquee output directory."""
    env = os.environ.get("GS_PORTFOLIO_CONFIG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return research_dir().parent / "portfolio_config.yaml"


def _universe_csv_path() -> Path:
    """The per-instrument ISA preclearance universe lives at the gs-summarize repo
    root (the parent of the marquee output directory)."""
    env = os.environ.get("GS_UNIVERSE_CSV", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return research_dir().parent / _UNIVERSE_CSV


def _date_from_name(p: Path) -> str:
    m = _DATE_RE.search(p.name)
    return m.group(1) if m else ""


def status() -> dict[str, Any]:
    """Bridge health: is the directory present, how many briefs, how fresh."""
    d = research_dir()
    briefs = sorted(d.glob(_BRIEF_GLOB)) if d.is_dir() else []
    latest = _date_from_name(briefs[-1]) if briefs else None
    stale_days = None
    if latest:
        try:
            stale_days = (date.today() - date.fromisoformat(latest)).days
        except ValueError:
            stale_days = None
    return {
        "available": bool(briefs),
        "research_dir": str(d),
        "brief_count": len(briefs),
        "latest_date": latest,
        "stale_days": stale_days,
        "compliance_config_present": _portfolio_config_path().is_file(),
        "preclearance_universe_present": _universe_csv_path().is_file(),
    }


# ── Briefs (markdown) ──────────────────────────────────────────────────────────


def list_briefs(limit: int = 30) -> list[dict[str, Any]]:
    """Available daily briefs, newest first: ``[{date, file}]``."""
    d = research_dir()
    if not d.is_dir():
        return []
    out = [{"date": _date_from_name(p), "file": p.name} for p in d.glob(_BRIEF_GLOB)]
    out = [r for r in out if r["date"]]
    out.sort(key=lambda r: r["date"], reverse=True)
    return out[:limit]


def get_brief(target_date: str | None = None) -> dict[str, Any]:
    """Return one daily brief's markdown. ``target_date`` (YYYY-MM-DD) or latest."""
    d = research_dir()
    if not d.is_dir():
        return {"available": False, "reason": f"GS research directory not found: {d}"}

    briefs = sorted(d.glob(_BRIEF_GLOB), key=_date_from_name)
    if not briefs:
        return {"available": False, "reason": f"No briefs in {d}"}

    chosen: Path | None = None
    if target_date:
        for p in briefs:
            if _date_from_name(p) == target_date:
                chosen = p
                break
        if chosen is None:
            return {
                "available": False,
                "reason": f"No brief for {target_date}",
                "available_dates": [_date_from_name(p) for p in reversed(briefs)][:10],
            }
    else:
        chosen = briefs[-1]

    try:
        text = chosen.read_text(encoding="utf-8")
    except OSError as exc:
        return {"available": False, "reason": f"Failed to read {chosen.name}: {exc}"}

    return {
        "available": True,
        "date": _date_from_name(chosen),
        "file": chosen.name,
        "markdown": text,
    }


# ── Structured analyses / trade ideas (JSON) ────────────────────────────────────


def _load_analyses(target_date: str | None) -> tuple[str | None, list[dict[str, Any]]]:
    d = research_dir()
    if not d.is_dir():
        return None, []
    files = sorted(d.glob(_ANALYSES_GLOB), key=_date_from_name)
    if not files:
        return None, []
    chosen: Path | None = None
    if target_date:
        for p in files:
            if _date_from_name(p) == target_date:
                chosen = p
                break
    else:
        chosen = files[-1]
    if chosen is None:
        return None, []
    try:
        data = json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("GS analyses read failed for %s: %s", chosen.name, exc)
        return _date_from_name(chosen), []
    return _date_from_name(chosen), (data if isinstance(data, list) else [])


def get_trade_ideas(target_date: str | None = None) -> dict[str, Any]:
    """Flatten the per-article structured trade ideas + key views into one payload.

    Pulls from ``analyses_{date}.json`` — the machine-readable companion to the
    markdown brief. Each idea carries its source article so Archie can cite it.
    """
    used_date, analyses = _load_analyses(target_date)
    if not analyses:
        return {
            "available": False,
            "reason": "No structured analyses found",
            "date": used_date,
        }

    ideas: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    risk_flags: list[str] = []
    tickers: set[str] = set()

    for a in analyses:
        if not isinstance(a, dict) or "_error" in a:
            continue
        src = a.get("_article_title", "")
        for t in a.get("trade_ideas", []) or []:
            if not isinstance(t, dict):
                continue
            ideas.append(
                {
                    "instrument": t.get("instrument"),
                    "direction": t.get("direction"),
                    "rationale": t.get("rationale"),
                    "target": t.get("target"),
                    "stop": t.get("stop"),
                    "source": src,
                }
            )
        for v in a.get("key_views", []) or []:
            if not isinstance(v, dict):
                continue
            views.append(
                {
                    "asset_or_theme": v.get("asset_or_theme"),
                    "direction": v.get("direction"),
                    "conviction": v.get("conviction"),
                    "summary": v.get("summary"),
                    "source": src,
                }
            )
        for rf in a.get("risk_flags", []) or []:
            if rf:
                risk_flags.append(str(rf))
        for tk in a.get("tickers_mentioned", []) or []:
            if tk:
                tickers.add(str(tk))

    return {
        "available": True,
        "date": used_date,
        "article_count": len(analyses),
        "trade_ideas": ideas,
        "key_views": views,
        "risk_flags": sorted(set(risk_flags)),
        "tickers_mentioned": sorted(tickers),
    }


# ── Compliance / preclearance rules (the user's own hedge-fund constraints) ─────


def get_compliance_rules() -> dict[str, Any]:
    """Read the user's trading constraints from ``portfolio_config.yaml``.

    This is the SAME file the gs-summarize synthesis uses, so editing it there
    is the single source of truth — Archie always sees the live preclearance
    ruleset. Returns the tradeable universe (what's free vs. preclearance-gated),
    any compliance notes, and preferences.
    """
    p = _portfolio_config_path()
    if not p.is_file():
        return {"available": False, "reason": f"portfolio_config.yaml not found at {p}"}
    try:
        import yaml

        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"Failed to parse {p}: {exc}"}

    universe = cfg.get("tradeable_universe", {}) if isinstance(cfg, dict) else {}
    counts = _universe_verdict_counts()
    return {
        "available": True,
        "source": str(p),
        "no_preclearance": universe.get("no_preclearance", {}),
        "requires_preclearance": universe.get("requires_preclearance", {}),
        "notes": universe.get("notes", []),
        "preferences": cfg.get("preferences", {}) if isinstance(cfg, dict) else {},
        # The category rules above are the policy; the per-instrument universe (when
        # present) is the authoritative verdict — resolve individual names with
        # check_preclearance() rather than guessing from the categories.
        "preclearance_universe": counts,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


# ── Per-instrument preclearance verdict (isa_universe.csv) ──────────────────────


def _load_universe() -> dict[str, Any]:
    """Load + index ``isa_universe.csv`` (T212 ticker → preclearance verdict).

    The companion repo's trade-automation classifier writes a per-instrument
    ``preclearance`` verdict (``no`` / ``review`` / ``yes``) plus a ``status_reason``
    tag. This is the authoritative source for "can Abhinav trade this without
    preclearance" — far more precise than the category rules in portfolio_config.yaml.
    Cached and invalidated on file mtime. Indexed by ticker, short_name, and isin
    (all upper-cased) for flexible lookup.
    """
    p = _universe_csv_path()
    try:
        mtime = p.stat().st_mtime if p.is_file() else None
    except OSError:
        mtime = None
    if mtime is None:
        _universe_cache.update({"mtime": None, "path": str(p), "rows": [], "index": {}})
        return _universe_cache
    if _universe_cache["mtime"] == mtime and _universe_cache["path"] == str(p):
        return _universe_cache

    rows: list[dict[str, str]] = []
    index: dict[str, list[int]] = {}

    def _add(key: str, i: int) -> None:
        k = (key or "").strip().upper()
        if k:
            index.setdefault(k, []).append(i)

    try:
        with p.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                i = len(rows)
                verdict = (row.get("preclearance", "") or "").strip().lower()
                rec = {
                    "ticker": (row.get("ticker", "") or "").strip(),
                    "isin": (row.get("isin", "") or "").strip(),
                    "type": (row.get("type", "") or "").strip(),
                    "short_name": (row.get("short_name", "") or "").strip(),
                    "name": (row.get("full_name", "") or "").strip(),
                    "currency": (row.get("currency", "") or "").strip(),
                    "preclearance": verdict
                    if verdict in ("no", "review", "yes")
                    else "unknown",
                    "status_reason": (row.get("status_reason", "") or "").strip(),
                }
                rows.append(rec)
                _add(rec["ticker"], i)
                _add(rec["short_name"], i)
                _add(rec["isin"], i)
    except OSError as exc:
        logger.warning("ISA universe read failed for %s: %s", p, exc)
        _universe_cache.update({"mtime": None, "path": str(p), "rows": [], "index": {}})
        return _universe_cache

    _universe_cache.update(
        {"mtime": mtime, "path": str(p), "rows": rows, "index": index}
    )
    return _universe_cache


def _universe_verdict_counts() -> dict[str, Any]:
    uni = _load_universe()
    rows = uni["rows"]
    if not rows:
        return {"available": False}
    counts = {"no": 0, "review": 0, "yes": 0, "unknown": 0}
    for r in rows:
        counts[r["preclearance"]] = counts.get(r["preclearance"], 0) + 1
    return {
        "available": True,
        "source": uni["path"],
        "total": len(rows),
        "by_verdict": counts,
    }


def _lookup_symbol(query: str) -> dict[str, Any] | None:
    """Resolve one symbol/ticker against the universe, most-restrictive verdict wins."""
    uni = _load_universe()
    rows, index = uni["rows"], uni["index"]
    if not rows:
        return None

    q = (query or "").strip().upper()
    if not q:
        return None

    hits: list[int] = []
    for key in (q, q.rsplit("_", 1)[0] if "_" in q else q):
        if key in index:
            hits = index[key]
            break
    if not hits:
        return None

    matches = [rows[i] for i in dict.fromkeys(hits)]  # de-dupe, keep order
    # Compliance gate: surface the most restrictive verdict across listings.
    worst = max(matches, key=lambda r: _VERDICT_RANK.get(r["preclearance"], 3))
    return {
        "verdict": worst["preclearance"],
        "preclearance_required": worst["preclearance"] != "no",
        "matched_ticker": worst["ticker"],
        "name": worst["name"],
        "type": worst["type"],
        "status_reason": worst["status_reason"],
        "match_count": len(matches),
    }


def check_preclearance(symbols: list[str] | str) -> dict[str, Any]:
    """Resolve preclearance verdicts for one or more symbols/T212 tickers.

    Accepts plain symbols (``SPY``), short names, ISINs, or full T212 instrument
    codes (``SLB_US_EQ``). For each, returns the authoritative verdict from
    ``isa_universe.csv`` (``no`` = trade freely, ``review`` = needs review,
    ``yes`` = preclearance required, ``unknown`` = not found → treat as gated).
    Degrades to ``available: False`` when the universe CSV is absent.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    symbols = [str(s).strip() for s in (symbols or []) if str(s).strip()]

    uni = _load_universe()
    if not uni["rows"]:
        return {
            "available": False,
            "reason": f"ISA preclearance universe not found: {_universe_csv_path()}",
        }

    results: dict[str, Any] = {}
    for s in symbols:
        hit = _lookup_symbol(s)
        results[s] = (
            hit
            if hit
            else {
                "verdict": "unknown",
                "preclearance_required": True,
                "matched_ticker": None,
                "name": None,
                "reason": "not found in ISA universe — treat as preclearance-required",
            }
        )
    return {
        "available": True,
        "source": uni["path"],
        "results": results,
    }
