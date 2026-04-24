# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Reasoning & Planning Protocol

Before taking any action (tool calls or responses), proactively reason through:

1. **Logical dependencies**: Resolve conflicts in this order: policy/mandatory constraints → order of operations → prerequisites → user preferences. Reorder user-requested steps if needed to maximize successful completion.
2. **Risk assessment**: What are the consequences? Will the new state cause future issues? For exploratory tasks, prefer calling tools with available info over asking the user unless Rule 1 determines otherwise.
3. **Abductive reasoning**: Identify the most logical root cause at each step. Look beyond obvious causes; hypotheses may require multiple steps to test. Prioritize by likelihood but don't discard low-probability causes prematurely.
4. **Outcome evaluation**: Does the previous observation require plan changes? If initial hypotheses are disproven, generate new ones.
5. **Information availability**: Draw from all sources — tools, policies, conversation history, and the user when needed.
6. **Precision**: Verify claims by quoting exact applicable information.
7. **Completeness**: Exhaustively incorporate all requirements, constraints, and preferences. Check all information sources before assuming something is not applicable.
8. **Persistence**: Don't give up until all reasoning is exhausted. On transient errors, retry unless an explicit retry limit is hit. On other errors, change strategy — never repeat the same failed call.

**Inhibit your response**: only act after all the above reasoning is complete.

## Working Style

### Planning
- Enter plan mode for any non-trivial task (3+ steps or architectural decisions).
- Write plans to `tasks/todo.md` with checkable items; check in before implementing.
- Think like a senior quant engineer: break down problems, consider edge cases, plan for maintainability.
- If something goes sideways, STOP and re-plan immediately.

### Subagents
- Use subagents liberally to keep the main context window clean.
- Offload research, exploration, and parallel analysis to subagents — one task per subagent.
- For complex problems, throw more compute at it via subagents.

### Self-Improvement
- After any correction from the user: update `tasks/lessons.md` with the pattern.
- Write rules that prevent the same mistake; ruthlessly iterate until mistake rate drops.
- Review `tasks/lessons.md` at session start for relevant context.

### Verification
- Never mark a task complete without proving it works.
- Run tests, check logs, demonstrate correctness.
- Ask: "Would a staff engineer approve this?"

### Code Quality
- For non-trivial changes, pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution."
- Skip for simple, obvious fixes — don't over-engineer.
- **Simplicity first**: make every change as small as possible; minimize code impact.
- **No laziness**: find root causes, no temporary fixes, senior developer standards.

### Bug Fixing
- When given a bug report: just fix it. Point at logs/errors/failing tests and resolve them.
- Fix failing CI tests without being told how.

## Task Tracking

1. Write plan to `tasks/todo.md` with checkable items.
2. Check in before starting implementation.
3. Mark items complete as you go.
4. Add a review section to `tasks/todo.md` when done.
5. Update `tasks/lessons.md` after any corrections.

---

## What this repo does

GEX (Gamma Exposure) data pipeline for TradingView Pine Seeds:

1. **`generate_gex.py`** — fetches options greeks + open interest from ThetaData Terminal (must be running locally on `localhost:25503`) and writes single-row OHLCV CSV files to `data/`.
2. **`data/*.csv`** — each file is one Pine Seeds symbol (e.g. `SPY_GEX_FLIP.csv`). The single row is forward-filled by TradingView to all chart bars.
3. **`gex_seeds.pine`** — Pine Script v6 indicator that reads those CSVs via `request.seed()` and renders a GEX histogram + key level lines on the chart.
4. **`run_updater.sh`** — runs the generator and auto-commits/pushes changed CSVs to GitHub. Intended to run on a cron every 5 minutes during market hours.
5. **`seeds.json`** — Pine Seeds symbol manifest (registered with TradingView).

## Running the generator

```bash
# Update default symbols (SPY)
python generate_gex.py

# Override symbols
python generate_gex.py SPY QQQ SPX

# Full update + git push (as cron does)
./run_updater.sh SPY QQQ
```

ThetaData Terminal must be running on `localhost:25503` before invoking. Python dependency: `httpx` (async HTTP client).

## Cron schedule

```
*/5 13-21 * * 1-5 /Users/garychang/tradingview/run_updater.sh >> /tmp/gex_updater.log 2>&1
```

(13:00–21:00 UTC = 9 AM–5 PM ET)

## CSV format

Each CSV has exactly one data row: `time, open, high, low, close, volume` where `time` is a UTC Unix timestamp and the value of interest is in the `close` column. Pine Seeds forward-fills the latest row, so only one row is ever needed.

## GEX calculation

`gex = gamma × open_interest × 100 × spot_price`

- Calls contribute positive GEX, puts contribute negative GEX.
- Aggregated across the nearest `NUM_EXPS` (4) expirations within `STRIKE_RANGE` (±10%) of spot.
- Outputs: Gamma Flip level (zero-crossing interpolation), Call Wall (max GEX strike), Put Wall (min GEX strike), top 10 GEX strikes, top 5 VEX (Vanna Exposure) strikes.

## Pine Script limits

`request.seed()` is capped at 40 calls per script. The indicator uses exactly 40: 7 key levels + 3 net scalars + 10 GEX strikes + 10 GEX values + 5 VEX strikes + 5 VEX values. `TOP_N = 10` and `VEX_TOP_N = 5` in `generate_gex.py` — do not increase without auditing the Pine Script call count first.

## Playwright tests

The `tests/` directory and `playwright.config.js` are boilerplate stubs — the `example.spec.js` tests point at playwright.dev and have no relation to this project.

```bash
npm install          # install Playwright
npx playwright test  # run tests (stubs only)
```
