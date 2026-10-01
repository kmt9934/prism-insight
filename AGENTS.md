# AGENTS.md - Agent Guide for PRISM-INSIGHT

This file governs the repository rooted here.

## Project Summary

PRISM-INSIGHT is an AI-powered Korean/US stock analysis and automated trading system built around:

- Python 3.10+
- GPT-5 / Claude based analysis agents
- SQLite storage
- Telegram delivery
- KIS trading APIs
- KR and US market flows

Primary source material for project context lives in `CLAUDE.md` and supporting docs under `docs/`.

## Repository Map

- `cores/`: main analysis engine, report generation, agent definitions, ChatGPT OAuth proxy
- `cores/agents/`: specialized analysis, communication, and trading agents
- `trading/`: Korean trading integration and account handling
- `prism-us/`: US market mirror flows and trading support
- `tracking/`: journal, memory, trading state helpers
- `messaging/`: Redis and GCP Pub/Sub messaging
- `tests/`: targeted regression tests
- `docs/`: setup, troubleshooting, and agent references

## Preferred Commands

Use targeted, low-side-effect commands first.

### Setup

```bash
pip install -r requirements.txt
python3 -m playwright install chromium
```

### Local analysis runs

```bash
python stock_analysis_orchestrator.py --mode morning --no-telegram
python prism-us/us_stock_analysis_orchestrator.py --mode morning
python demo.py 005930
python demo.py AAPL --market us
python weekly_insight_report.py --dry-run
```

### Focused tests

```bash
pytest tests/test_trading_journal.py
pytest tests/test_tracking_agent.py
pytest tests/test_portfolio_reporter.py
pytest tests/test_multi_account_domestic.py
```

Avoid broad production-like runs unless the task requires them.

## Change Rules

- Default to safe paths: prefer `--no-telegram`, `--dry-run`, demo mode, or isolated tests.
- Do not change or commit real credentials, tokens, or secrets in `.env`, `mcp_agent.secrets.yaml`, or `trading/config/kis_devlp.yaml`.
- Treat generated logs, PDFs, JSON outputs, and SQLite databases as user data unless the task explicitly targets them.
- Keep changes narrow and consistent with existing patterns; this repo has substantial behavior encoded in prompts and orchestration order.

## Authorized PR merge and deployment workflow

- User instruction (2026-09-06): for PRISM-INSIGHT task-scoped, validated changes,
  proceed with merge and deployment without waiting for a separate approval review.
- The main branch requires zero approving reviews by explicit user request.
  Preserve the PR requirement, CI/status checks, and all other branch protections.
- Check the exact PR head, dependency order, relevant tests, and CI before merging.
- Any feature change, incident repair, or deployment request must first apply the
  change-scope verification gates in docs/SERVER_GIT_OPERATIONS_ko.md. This is
  mandatory task routing, not optional memory recall. Record pre-deploy tests,
  post-deploy smoke, and whether the first scheduled run has actually completed.
  Do not interpret the review waiver as permission to merge failing or unrelated work.
- Follow docs/SERVER_GIT_OPERATIONS_ko.md: clean target, verified commit, ff-only
  deployment, no destructive reset/stash/clean, and no credential/runtime-data edits.
- BTC deployment remains Bybit demo unless the user separately approves real funds.
  New risk-budget defaults, strategy promotion, and unrelated app-server deployments
  are not authorized merely by this review-workflow preference.
- See docs/BTC_TPSL_DEPLOYMENT_2026-09-06_ko.md for the authorization and initial rollout evidence.

## mini2 live operations (kmt9934 fork)

### Live tree and deploy

- Live deploy tree: mini2 `/home/user/services/prism-insight` (ssh `user@mini2`), a detached
  checkout of the fork (remote `fork` on mini2 = kmt9934/prism-insight). It is not upstream and
  differs from upstream main. It also holds live-only state that exists in no remote: `.env`
  (git-ignored), an untracked `docker-compose.override.yml`, and runtime data (reports, logs,
  `stock_tracking_db.sqlite`, etc.). Never reset/clean/stash or overwrite it; check `git status`
  and `git log` on mini2 before assuming it matches any remote.
- Deploy flow (`tools/deploy_live.sh`, `tools/deploy_preflight.py`, `docs/DEPLOY_LIVE.md`): edit on
  mini1, commit, push to the fork, then on mini2 run `tools/deploy_live.sh [REF]` (default
  `fork/main`). The script refuses tracked modifications, fetches, checks out REF detached, builds a
  candidate image, runs `tools/deploy_preflight.py` inside it with `--network none` (import/name
  resolution, entrypoint imports with sockets blocked, call-site signatures, CRLF checks; no orders,
  broker calls or Telegram), and only then retags `prism-insight:latest` and recreates
  `prism-insight-container`. On build/preflight failure it restores the previous checkout and
  leaves containers untouched. It ends with `tools/check_live_config.sh`.
- Rollback = `tools/deploy_live.sh <previous commit>` (the script prints it).
- The preflight is not pytest: run the relevant pytest on mini1 before pushing and record results
  per `docs/SERVER_GIT_OPERATIONS_ko.md`. Never copy individual files into the live tree; deploy
  whole commits.
- `prism-telegram-bot` stays off: mini2's untracked `docker-compose.override.yml` puts it in profile
  `chat-disabled` with `restart: "no"`, and the container does not exist. Do not run
  `deploy_live.sh --with-bot` or start/recreate that service (naming it explicitly would start it)
  without explicit user approval.

### `.env`, strategy switches and secrets

- Before editing `.env`, back it up to `/home/user/prism_backups/`
  (`cp -p .env /home/user/prism_backups/.env.bak-$(date +%Y%m%d-%H%M%S)-<reason>`).
- After ANY `.env` or `docker/crontab` change run
  `docker compose up -d --force-recreate prism-insight`, then `tools/check_live_config.sh` (must show
  OK; it prints file names and OK/STALE only). Reason: single-file bind mount
  `./.env:/app/prism-insight/.env` (`docker-compose.yml`); editors that replace the file leave the
  container on the old inode.
- REGIME_* values are read from the mounted `.env` via `load_dotenv()` (`stock_tracking_agent.py`,
  `stock_analysis_orchestrator.py`, `trigger_batch.py`), not from the container environment (compose
  `environment:` has no REGIME_* and no `env_file`). Check the file, not `docker exec ... env`.
  KR parsing is lenient via `prism_core/env_config.py`.
- Strategy switches (owner rule): the only `.env` values an agent may change as strategy switches
  are `REGIME_MIN_SCORE_FLOOR`, `REGIME_WEAK_NO_TOPDOWN` and `REGIME_HIVOL_OVERRIDE`, and only when
  the user asks. Any other strategy/regime/entry/exit/sizing change goes through
  `docs/TRADING_CHANGE_REVIEW_HARNESS.md`. `REGIME_HIVOL_OVERRIDE=active` is the stricter setting and
  the code default (`cores/data_prefetch.py`, `prism-us/cores/data_prefetch.py`); `shadow`/`off` are
  emergency rollback (`docs/FEATURE_FLAGS.md`).
- Never print `.env` values when reporting; report names and on/off only if the user asks.
- Never log, print or echo the Telegram bot token or any other secret (`.env`,
  `mcp_agent.secrets.yaml`, `trading/config/kis_devlp.yaml`). Commit 8c370a3 added
  `prism_core/log_redaction.py`; entrypoints call `install_log_redaction()` after logging is
  configured (quiets httpx/httpcore/telegram loggers to WARNING and redacts bot tokens). Any new
  entrypoint or tool must do the same; do not raise httpx logging back to INFO/DEBUG.
- Luna cron: the `tools/buy_intensity_luna_recommend.py` line in `docker/crontab` is intentionally
  commented out (`DISABLED 2026-09-29`). Do not re-enable it.

### Git and known gaps

- Commit and push only to the fork (`kmt9934` remote on mini1, `fork` on mini2); PRs target
  kmt9934/prism-insight `main`. Never push to or open a PR against upstream
  `dragon1086/prism-insight` (PR #753 was opened there by mistake and closed unmerged on
  2026-09-22 KST).
- `pykrx` is not installed in the live container or on the mini2 host and is not in
  `requirements.txt`; KR market data comes through the `kospi_kosdaq` MCP server launched with
  `uvx` (see `mcp_agent.config.yaml.example`). Do not write code or scripts that assume
  `import pykrx` works.
- `uv`/`uvx` exist only inside the container at `/root/.local/bin`, not on the mini2 host.
  `docker/crontab` PATH includes `/root/.local/bin` (commit d16f814); keep it.
- The container runs as root and writes into bind-mounted folders, so parts of the live tree are
  root-owned (e.g. `prism-us/reports`, `prism-us/pdf_reports`, many files under `reports/`, some
  `__pycache__`). Do not chown/rm them as a side task; expect checkouts/cleanups touching them to
  fail, and report instead.

## Engineering Rules

### Async and I/O

- In async flows, use non-blocking patterns.
- Do not introduce blocking network calls such as `requests.get(...)` inside async execution paths; use the repo's async approach instead.

### Agent execution

- Preserve sequential execution of analysis agents unless there is clear existing infrastructure for safe parallelism.
- Do not replace sequential report generation with `asyncio.gather(...)` for LLM-heavy sections; rate limits and prompt ordering matter here.
- Market analysis may use cache-aware behavior; preserve that pattern when editing orchestration.

### Trading and data safety

- Default trading behavior should remain safe (`demo` unless explicitly required otherwise).
- Preserve portfolio constraints and stop-loss logic unless the task explicitly changes trading rules.
- When parsing KIS API numeric fields, prefer existing safe conversion helpers over direct casts.
- Before changing screening, regime, entry, exit, or sizing behavior, follow
  `docs/TRADING_CHANGE_REVIEW_HARNESS.md`. In particular, do not generalize a
  trigger-local failure into a global hard gate without testing counterexamples.
- When a request proposes introducing, combining, replacing, or retuning a
  strategy, trigger, factor, regime switch, exit, or sizing rule (including an
  LLM prompt that changes economic decisions), apply the Strategy adoption and
  fit gate in that harness before implementation. Natural-language proposals
  count; no special command is required. Compatibility review must trace actual
  screening code through report inputs, BUY/SELL prompts, deterministic gates,
  execution and exit management; conceptual strategy fit or prompt-only review
  is insufficient. Record intended stage differences versus contradictions,
  source locations and same-candidate tests before claiming compatibility.
  Keep execution/data bug repair separate
  and do not delay a proven safety fix under the guise of strategy research.

### BTC roadmap governance

- Before BTC strategy, execution, backtest, risk, observability, or deployment work,
  read `docs/BTC_ROADMAP_ko.md` and identify the milestone/task ID and prerequisites.
- Follow its evidence gates; keep execution-safety fixes separate from strategy experiments.
- At completion, update roadmap status only with verified evidence and distinguish
  implementation, tests, deployment, forward observation, and profitability proof.
- Agentmemory is a pointer/reminder, not the authoritative roadmap or status ledger.
- Roadmap review is development-time only; do not add it to the live trading loop.

### Entry-quality analysis

- Route requests such as “매수품질 데이터 분석해줘”, entry-quality validation,
  trigger comparison/replacement, replay, or entry-rule promotion review through
  `skills/prism-entry-quality-analysis/SKILL.md`.
- Read `docs/ENTRY_QUALITY_EVOLUTION_ko.md` and
  `docs/ENTRY_QUALITY_DATA_ANALYSIS_HARNESS.md` before interpreting the data.
- Generate a deterministic Evidence Packet with
  `tools/build_entry_quality_evidence_packet.py`; do not substitute ad-hoc SQL,
  fuzzy joins, or reconstructed fills.
- Keep `MISSING` as unknown. Strategy-ledger entries/exits are independent of broker
  funding and fills; do not exclude valid strategy outcomes because a broker order
  was rejected. Broker-realized PnL requires separate confirmed entry/exit evidence.
- Never promote a trigger or entry-quality rule to SHADOW or LIVE automatically.
  LIVE requires the trading change harness and explicit user approval.

### Report output

- BTC trade notifications must follow `docs/BTC_POSITION_MESSAGE_CONTRACT_20260915_ko.md`:
  preserve detailed entry/add/reduction/exit/recovery context, verified margin mode,
  exchange leverage, position margin and same-account equity ratio with currency
  and capture time. Never substitute strategy capital/exposure for broker figures,
  attach another position's snapshot to an old exit, or delay protection/execution
  for optional notification enrichment. Missing data stays explicitly unknown.
  Public notices must be compact and event-specific: do not append raw account
  snapshots or repeated unknown fields to exits. Keep full diagnostic data, group
  essential missing-data warnings, and never hide unconfirmed settlement status.
- Korean report text must use formal polite style.
- Preserve existing prompt and report structure unless the task explicitly requests prompt/report redesign.

## File-Specific Notes

- `cores/report_generation.py`: common report tone and section formatting rules
- `cores/analysis.py`: sequential orchestration and section integration
- `cores/agents/*.py`: prompt logic and agent responsibilities
- `stock_tracking_agent.py`: trading loop, sell decisions, optional journal flow
- `telegram_ai_bot.py`: user consultation flows and conversation context

## Before Finishing

- Run the smallest relevant test or command that validates the change.
- For screening/provider changes, unit tests or AST-extracted tests alone are insufficient:
  run real-module KR consumer and US morning/afternoon batch-to-JSON integration
  tests with realistic flat/MultiIndex/empty/ambiguous provider fixtures. Keep
  network, broker orders and channel sends disabled in integration tests. After
  deployment, verify the production Python and a bounded read-only provider
  smoke before claiming operational recovery; do not rerun live orders as a test.
- If you could not run validation, say so explicitly and explain why.
- In summaries, reference the files changed and note any operational risk, especially around trading, messaging, or credential handling.
