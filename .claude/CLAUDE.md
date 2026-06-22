# MyPF — Development Context

## Architecture
- **Frontend**: React (Vite) — `frontend/src/`
- **Backend**: FastAPI + SQLite — `backend/app/`
- **Chat runtime**: Claude Agent SDK with project setting source enabled
- **Archie's runtime memory**: `.claude/runtime/memory/` (managed by Archie, not dev tooling)

## Key Directories
| Path | Purpose |
|------|---------|
| `frontend/src/components/` | React UI components |
| `frontend/src/api/` | API client layer |
| `backend/app/services/` | Core backend services (chat runtime, memory, portfolio) |
| `backend/app/routers/` | FastAPI route handlers |
| `.claude/runtime/.claude/CLAUDE.md` | Archie's identity + memory map (SDK reads this) |
| `.claude/runtime/memory/` | Archie's persistent memory files |

## Dev Workflow

**Prerequisites**: Python 3.11+, Node.js 18+, an Anthropic API key, and (optional) Trading 212 API keys.

**First-time setup** (`./dev.sh` prints these and seeds `.env` files from `.env.example`):
```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cd frontend && npm install
```
Copy `backend/.env.example` → `backend/.env` and `frontend/.env.example` → `frontend/.env`, then fill in keys.

**Run locally** (three processes):
```bash
# Backend API  — http://localhost:8000  (docs at /docs)
cd backend && ../.venv/bin/uvicorn app.main:app --reload --port 8000
# Worker (scheduled tasks + Telegram polling)
cd backend && ../.venv/bin/python worker.py
# Frontend — http://localhost:5173
cd frontend && npm run dev
```

**Run via Docker** (from repo root): `make up` (hot-reload, default) · `make up MODE=stable` · `make logs` · `make restart` · `make down`.

**Frontend build/preview**: `npm run build` (runs `tsc -b && vite build`) · `npm run preview`.

**Tests** (pytest, run from `backend/`):
```bash
cd backend && ../.venv/bin/python -m pytest          # full suite
cd backend && ../.venv/bin/python -m pytest tests/test_agent_routing.py   # single file
```

**Lint/format (Python)** — run after any backend change:
```bash
ruff format .
ruff check --fix .
```

**Key env vars**: `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `T212_API_KEY_INVEST`, `T212_API_KEY_STOCKS_ISA`, `T212_BASE_ENV` (`demo`|`live`), `CLAUDE_MEMORY_STRATEGY`. See `backend/.env.example` for the full list.

## Conventions
- Chat UI should be clean and information-dense
- Assistant responses render markdown clearly (tables, lists, headers)
- Tool activity visible when relevant
- Presentation mode obfuscates sensitive numeric portfolio values
- Default display currency: GBP

## Agent Capabilities (for prompt/context work)
- **MCP tool servers**: `marketdata` (price/history/technicals/risk/correlation/compare),
  `fundamentals` (fundamentals/valuation/statements/earnings), `forecast` (Kronos p10/p50/p90),
  `scheduler` (cron tasks), `trading212` (account/orders).
- **Subagents**: researcher (Sonnet — web + marketdata + fundamentals), quant (Opus — Bash +
  `app.quant` + marketdata + forecast), execution (Haiku — T212 orders).
- **Leveraged engine**: regime-aware 3x ISA ETP scan/monitor/execute under hard daily rails.
  T212 has **no short selling** — downside is via **INVERSE ETPs (ISA-only)**.
- **Autonomous loop**: scheduled morning cycle, midday/EOD monitors, weekly review, daily alpha goal.

## Risk Guardrails
- Never suggest bypassing configured risk rails (daily profit/loss/trade caps are hard-enforced)
- Always call out concentration, liquidity, and downside risk
- Distinguish analysis from execution; never imply trades are executed unless confirmed

## User Context
- User name: Abhinav
- Accounts: Trading 212 Invest + Stocks ISA
- Communication style: friendly, reliable, highly knowledgeable, conversational but pragmatic
