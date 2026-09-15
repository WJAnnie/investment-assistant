# Global Market Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the morning report's global-market placeholder with strictly validated public observations for eight exact instruments while preserving WAIT-only, synthetic-account, and secret-isolation boundaries.

**Architecture:** Add a focused Yahoo/FRED adapter that returns typed source observations, then let the public-trial boundary convert them into a strict `public-trial/v2` allowlisted snapshot. Read legacy v1 snapshots during migration, write v2 only, and degrade each instrument independently without deriving trading actions.

**Tech Stack:** Python 3.11 standard library, `requests==2.32.5`, `unittest`, GitHub Actions, existing Feishu notifier.

---

## File map

- Create `app/market/global_markets.py`: exact instrument catalog, Yahoo Chart parser, FRED DGS10 parser, bounded HTTP behavior, typed successful observations.
- Create `tests/test_global_markets.py`: adapter-facing behavior and malformed/hostile response cases with local fixtures only.
- Modify `app/integration/public_trial.py`: v2 global records, v1 migration reader, per-instrument fallback orchestration, morning rendering.
- Modify `tests/test_public_trial.py`: v2 contract, v1 compatibility, partial/global failure and readable notification behavior.
- Modify `tests/test_public_trial_delivery.py`: notification bundle assertions for global observations.
- Modify `tests/test_public_trial_workflow.py`: sparse-checkout and no-secret collection gates.
- Modify `.github/workflows/public-trial.yml`: include the adapter/tests in sparse checkout and cloud regression run.
- Modify `README.md` and `docs/public-github-trial.md`: state the implemented source and its unverified/availability limits.

### Task 1: Parse exact Yahoo instruments

**Files:**
- Create: `app/market/global_markets.py`
- Create: `tests/test_global_markets.py`

- [ ] **Step 1: Write the failing success-path test**

Create a fake response/session and assert the public adapter returns the last two valid daily closes, source time, session date, and exact symbol:

```python
def test_yahoo_returns_latest_and_previous_valid_daily_close(self):
    session = FakeSession(yahoo_payload("^GSPC", [100.0, None, 102.0]))
    quote = YahooGlobalMarketProvider(session=session).fetch("^GSPC")
    self.assertEqual(quote.symbol, "^GSPC")
    self.assertEqual(quote.value, 102.0)
    self.assertEqual(quote.previous_value, 100.0)
    self.assertEqual(quote.session_date, "2026-09-14")
    self.assertEqual(quote.source_as_of, "2026-09-14T20:00:00+00:00")
    self.assertEqual(session.calls[0]["timeout"], 10)
```

- [ ] **Step 2: Run the test and confirm RED**

Run: `python -m unittest tests.test_global_markets.GlobalMarketProviderTests.test_yahoo_returns_latest_and_previous_valid_daily_close -v`

Expected: `ModuleNotFoundError: No module named 'app.market.global_markets'`.

- [ ] **Step 3: Implement the minimal typed provider**

Define the fixed catalog and immutable source observation:

```python
from dataclasses import dataclass

INSTRUMENTS = {
    "^GSPC": ("us_equities", "标普500", "points", "America/New_York", "INDEX"),
    "^DJI": ("us_equities", "道琼斯工业指数", "points", "America/New_York", "INDEX"),
    "^IXIC": ("nasdaq", "纳斯达克综合指数", "points", "America/New_York", "INDEX"),
    "^SOX": ("semiconductors", "费城半导体指数", "points", "America/New_York", "INDEX"),
    "^TNX": ("us_treasury", "美国10年期国债收益率", "percent", "America/Chicago", "INDEX"),
    "DX-Y.NYB": ("usd", "美元指数", "points", "America/New_York", "INDEX"),
    "GC=F": ("gold", "COMEX黄金", "usd_per_ounce", "America/New_York", "FUTURE"),
    "CL=F": ("oil", "WTI原油", "usd_per_barrel", "America/New_York", "FUTURE"),
}

@dataclass(frozen=True)
class SourceObservation:
    symbol: str
    value: float
    previous_value: float
    source_as_of: str | None
    session_date: str
    source: str
```

Implement `YahooGlobalMarketProvider.fetch(symbol)` using the fixed host, `urllib.parse.quote(symbol, safe="")`, `interval=1d`, `range=5d`, a 10-second timeout, redirects disabled by the supplied session, a 1 MiB body limit, exact response-symbol/instrument-type checks, finite positive values, and the final two valid `(timestamp, close)` pairs. Use `meta.regularMarketTime` for `source_as_of` and the last daily timestamp in the catalog timezone for `session_date`.

- [ ] **Step 4: Run the tracer test and confirm GREEN**

Run: `python -m unittest tests.test_global_markets.GlobalMarketProviderTests.test_yahoo_returns_latest_and_previous_valid_daily_close -v`

Expected: one test passes.

- [ ] **Step 5: Add one malformed-response case at a time**

Add and run tests for exact symbol mismatch, instrument-type mismatch, redirect/429/non-200, body over 1 MiB, malformed JSON, `chart.error`, fewer than two valid closes, non-finite/zero values, missing `regularMarketTime`, and a source time beyond Python's datetime range. Every case must raise `GlobalMarketDataError` without embedding response text or URL.

Run: `python -m unittest tests.test_global_markets -v`

Expected: all adapter tests pass and perform no real network traffic.

- [ ] **Step 6: Commit the vertical slice**

```text
git add app/market/global_markets.py tests/test_global_markets.py
git commit -m "Make exact overnight instruments independently observable" \
  -m "Constraint: Keyless public data may fail or change shape" \
  -m "Confidence: high" -m "Scope-risk: narrow" \
  -m "Tested: Offline Yahoo parser success and hostile-response regressions"
```

### Task 2: Add FRED's narrow Treasury fallback

**Files:**
- Modify: `app/market/global_markets.py`
- Modify: `tests/test_global_markets.py`

- [ ] **Step 1: Write the failing FRED parser test**

```python
def test_fred_returns_last_two_numeric_dgs10_observations(self):
    csv_body = b"observation_date,DGS10\n2026-09-10,4.95\n2026-09-11,4.96\n2026-09-14,.\n"
    quote = FredTreasuryProvider(session=FakeSession(csv_body)).fetch("^TNX")
    self.assertEqual(quote.value, 4.96)
    self.assertEqual(quote.previous_value, 4.95)
    self.assertEqual(quote.session_date, "2026-09-11")
    self.assertIsNone(quote.source_as_of)
    self.assertEqual(quote.source, "fred_dgs10")
```

- [ ] **Step 2: Run the test and confirm RED**

Run: `python -m unittest tests.test_global_markets.GlobalMarketProviderTests.test_fred_returns_last_two_numeric_dgs10_observations -v`

Expected: `NameError` or import failure for `FredTreasuryProvider`.

- [ ] **Step 3: Implement only the DGS10 adapter**

Use `https://fred.stlouisfed.org/graph/fredgraph.csv`, fixed `id=DGS10`, 10-second timeout and 1 MiB limit. Parse with `csv.DictReader`, accept only ISO dates and finite positive values, skip FRED's `.` missing marker, require two valid rows, and reject any symbol other than `^TNX`.

- [ ] **Step 4: Add failure tests and run GREEN**

Test wrong CSV headers, one valid row, invalid date, non-finite value, redirect/non-200 and oversized body.

Run: `python -m unittest tests.test_global_markets -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```text
git add app/market/global_markets.py tests/test_global_markets.py
git commit -m "Keep Treasury observations available without inventing intraday time" \
  -m "Constraint: FRED DGS10 is date-only and delayed" \
  -m "Confidence: high" -m "Scope-risk: narrow" \
  -m "Tested: Offline FRED parsing and fail-closed boundary cases"
```

### Task 3: Introduce the strict v2 global snapshot

**Files:**
- Modify: `app/integration/public_trial.py`
- Modify: `tests/test_public_trial.py`

- [ ] **Step 1: Write a failing v2 morning behavior test**

Inject fake Yahoo/FRED providers and assert a morning snapshot has eight fixed records, `%` changes for seven market instruments, `bp` for Treasury, and no trade action:

```python
def test_v2_morning_collects_exact_global_instruments_without_enabling_trade(self):
    payload = build(global_provider=FakeGlobalProvider(), treasury_fallback=FailingProvider())
    self.assertEqual(payload["schema_version"], "public-trial/v2")
    self.assertEqual([q["symbol"] for q in payload["global_market"]["quotes"]], list(trial.GLOBAL_SYMBOLS))
    treasury = next(q for q in payload["global_market"]["quotes"] if q["symbol"] == "^TNX")
    self.assertEqual(treasury["change_unit"], "bp")
    self.assertAlmostEqual(treasury["change"], 1.0)
    self.assertEqual(payload["decision"]["action"], "WAIT")
    self.assertFalse(payload["decision"]["auto_trade_enabled"])
```

- [ ] **Step 2: Run the test and confirm RED**

Run: `python -m unittest tests.test_public_trial.ContractTests.test_v2_morning_collects_exact_global_instruments_without_enabling_trade -v`

Expected: missing `global_provider` argument or `global_market` field.

- [ ] **Step 3: Add v2 collection and strict records**

Add `global_market` to newly written snapshots with fixed record keys:

```python
{
    "category": category,
    "symbol": symbol,
    "name": name,
    "value": value,
    "previous_value": previous_value,
    "value_unit": value_unit,
    "change": change,
    "change_unit": "bp" if symbol == "^TNX" else "pct",
    "source": source,
    "source_as_of": source_as_of,
    "session_date": session_date,
    "freshness": freshness,
    "error_code": error_code,
}
```

For Yahoo observations, compute Treasury change as `(value - previous_value) * 100` and every other change as `(value / previous_value - 1) * 100`. Use `RECENT` for age `<=36h`, `DELAYED_OR_HOLIDAY` for `<=120h`, `STALE` above `120h`, and reject source times more than five minutes in the future. For date-only FRED observations, calculate age from Shanghai 23:59:59 on `session_date` without populating `source_as_of`.

Call the FRED fallback only when `^TNX` Yahoo collection fails. Do not call either global provider for non-morning stages; emit fixed `NOT_COLLECTED_FOR_STAGE` records and status.

- [ ] **Step 4: Add per-symbol degradation tests incrementally**

Test Yahoo failure for one non-Treasury symbol, Yahoo failure plus successful FRED Treasury fallback, both Treasury sources failing, stale data, future source time, all sources failing, and non-morning zero-call behavior. Assert global status is exactly `complete`, `partial`, `unavailable`, or `not_collected`.

- [ ] **Step 5: Add strict v1 read compatibility**

Freeze the old v1 expected structure in a dedicated validator. `validate_snapshot` dispatches only on exact schema values `public-trial/v1` and `public-trial/v2`; write/build paths produce v2 only. Add tests that old valid fixtures read successfully, v1 with any v2 field fails, v2 missing/extra/reordered records fails, and mixed v1/v2 report directories render without mutation.

- [ ] **Step 6: Run public-trial tests and commit**

Run: `python -m unittest tests.test_public_trial -v`

Expected: all tests pass.

```text
git add app/integration/public_trial.py tests/test_public_trial.py
git commit -m "Make global market evidence auditable in public snapshots" \
  -m "Constraint: Existing v1 public data must remain strictly readable during migration" \
  -m "Confidence: high" -m "Scope-risk: moderate" \
  -m "Directive: Never infer trade permission from global quote completeness" \
  -m "Tested: v2 schema, v1 migration, freshness, partial failure, and WAIT boundaries"
```

### Task 4: Render user-facing morning data and update Actions

**Files:**
- Modify: `app/integration/public_trial.py`
- Modify: `tests/test_public_trial.py`
- Modify: `tests/test_public_trial_delivery.py`
- Modify: `tests/test_public_trial_workflow.py`
- Modify: `.github/workflows/public-trial.yml`

- [ ] **Step 1: Write a failing rendering test**

Assert the report contains the seven headings/data groups, source date/time, `%` and `bp`, but not internal values:

```python
def test_morning_renders_global_observations_for_a_user_not_an_operator(self):
    rendered = trial.render_report(build(global_provider=FakeGlobalProvider()))
    for text in ("🌍 隔夜全球市场", "标普500", "纳斯达克综合指数", "费城半导体指数",
                 "美国10年期国债收益率", "美元指数", "COMEX黄金", "WTI原油",
                 "bp", "数据时间"):
        self.assertIn(text, rendered)
    for internal in ("yahoo_chart", "fred_dgs10", "RECENT", "public-trial/v2"):
        self.assertNotIn(internal, rendered)
```

- [ ] **Step 2: Run RED, implement rendering, then run GREEN**

Replace the placeholder line with a compact global section. Format index points with grouping separators, yields with `%`, commodities with their USD unit, and each failure as `暂不可用`. Show `截至 MM-DD HH:MM` when `source_as_of` exists and `数据日 MM-DD` for FRED. End partial sections with a human-readable completeness warning. Keep the existing `【当前建议】 WAIT｜暂不操作` and risk statement.

Run: `python -m unittest tests.test_public_trial tests.test_public_trial_delivery -v`

Expected: both suites pass.

- [ ] **Step 3: Update the sparse checkout and workflow gates**

Add `/app/market/global_markets.py` and `/tests/test_global_markets.py` to sparse checkout. Run `python -m unittest tests.test_global_markets -v` in the offline boundary step. Keep all `secrets.*` references confined to the notification step.

- [ ] **Step 4: Run workflow-contract tests and commit**

Run: `python -m unittest tests.test_public_trial_workflow -v`

Expected: workflow parser tests pass, global adapter test is included, and no collection secret is detected.

```text
git add app/integration/public_trial.py tests/test_public_trial.py tests/test_public_trial_delivery.py tests/test_public_trial_workflow.py .github/workflows/public-trial.yml
git commit -m "Show overnight evidence as a readable morning brief" \
  -m "Constraint: Notifications are user messages, not execution logs" \
  -m "Confidence: high" -m "Scope-risk: moderate" \
  -m "Tested: Rendering, delivery bundle, sparse checkout, and secret-isolation regressions"
```

### Task 5: Document, verify, review, and smoke test

**Files:**
- Modify: `README.md`
- Modify: `docs/public-github-trial.md`

- [ ] **Step 1: Update documentation precisely**

Document exact symbols, Yahoo's unverified/keyless status, FRED's date-only DGS10 fallback, time/holiday caveats, per-symbol degradation, no-trade boundary, and the fact that one successful smoke test does not establish an SLA.

- [ ] **Step 2: Run the complete offline verification**

Run:

```text
python -m unittest discover -s tests -v
python -m compileall -q app tests
git diff --check
```

Expected: all tests pass, compileall exits 0, and diff check is clean.

- [ ] **Step 3: Run a read-only live smoke test**

Run `public_trial collect --stage morning --mode manual_replay` into a newly created temporary directory, validate it, print only fixed symbol/status/time/value fields, and remove the temporary directory after confirming its resolved absolute path is inside the OS temporary root. Do not send a notification in this step.

Expected: at least one valid global observation; any failed symbol has a fixed error code and the process still produces a valid WAIT snapshot.

- [ ] **Step 4: Perform root-agent review and evidence-based verification**

The root Codex session reviews the actual diff for parser trust boundaries, timestamps, unit semantics, v1/v2 migration, secret isolation, and failure containment. Antigravity may only be assigned concrete code/test fixes that modify files and run tests; it must not receive a read-only review task. Fix every confirmed issue, rerun the focused plus full suites, and perform the bounded live smoke test before completion.

- [ ] **Step 5: Commit documentation and verification state**

```text
git add README.md docs/public-github-trial.md
git commit -m "Set honest expectations for public global observations" \
  -m "Constraint: Keyless endpoints provide no project-controlled availability guarantee" \
  -m "Confidence: high" -m "Scope-risk: narrow" \
  -m "Tested: Full unittest suite, compileall, diff check, and read-only live smoke" \
  -m "Not-tested: Long-term provider SLA and device-level Feishu receipt"
```

- [ ] **Step 6: Inspect the final range**

Run: `git status --short && git log --oneline 389da4f..HEAD && git diff --stat 389da4f..HEAD`

Expected: clean worktree, reviewable commits, and only the files listed by this plan changed.
