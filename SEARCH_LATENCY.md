# Why search can take minutes

Baseline investigation: 2026-10-23, working tree based on `f34821f4` (the deadline
migration). Related follow-ups are in [OPEN_ISSUES.md](OPEN_ISSUES.md).

Implementation follow-up: search now defaults to discovery rather than acquiring
pages, and includes an optional Kagi Quick Answer (`quick_answer: true` by
default). Set `quick_answer: false` for links/snippets only, with no generation
or retained-memory lookup. The measurements and source line references below
describe the pre-change behavior, without Quick Answer generation.

## Conclusion

There is no fixed 197-second search delay. The default tool is a **batch browser
retrieval workflow**, not just a search-engine lookup. It waits for all requested
result pages, admits at most four acquisitions globally and one per origin, and
allows navigation/challenge continuation to consume the shared 300-second
request budget. One slow source can therefore hold the response open long after
other sources are ready.

This mechanism is verified in current source and isolated timing probes. Real
Pi history supports browsing as the primary latency suspect. **The exact phase
responsible for the 87–197 second production calls is not established:** the
server log has only total tool duration, and corresponding OpenCode history was
not available. Do not call this a demonstrated Kagi, Cloudflare, CPU or memory
failure without a phase trace.

## Production observations and limits of attribution

`kube.log` records:

| Log line | Search outcome | Elapsed |
| --- | --- | ---: |
| 11 | completed | 125.613 s |
| 50–51 | cancelled or failed; request cancellation logged | 197.263 s |
| 155 | completed | 196.675 s |
| 157 | completed | 9.426 s |
| 159 | completed | 7.685 s |
| 161 | completed | 87.488 s |

`completed` means the MCP result returned, not that every source succeeded
(`src/server.py:199–220`; per-source errors can be inside a successful result).
The log does not disclose the query, mode, result count, source cache states,
per-phase durations or deployed revision. These unknowns prevent reconstructing
a particular request from the log alone.

The first request starts at line 9; Chrome is ready at line 10, 26.4 seconds
later. The result arrives another 99.2 seconds after readiness. Cold startup
contributes, but cannot explain that whole call. The 196.7-second call at lines
154–155 has no intervening cold startup: Chrome was already ready at line 150.
The separate profile-retirement exceptions occurred during idle periods and are
not proof of the cause of these search durations.

The accessible Pi calls are from October 13–21 and the server log is October
22–23; they are different populations, not matching client/server traces.

## What the current search actually does

```text
one inherited request deadline
  -> CLI admission (two discovery/summary slots)
  -> pinned CLI version check + Kagi search
  -> unless content_mode=discovery:
       concurrent cache checks for every returned URL
       -> cache misses/refreshes enter shared acquisition admission
          <=4 active globally, <=1 per origin
       -> native navigation and challenge continuation
       -> rendered-DOM settling + bounded expansion
       -> serialization/extraction/PDF parsing + page cleanup
       -> cache write and background memory-ingestion submission
       wait for EVERY URL task
  -> optional generated summaries (summary mode only)
  -> optional retained-memory query (ordinary freshness only)
  -> redaction, highlight selection and response budgeting
  -> return one MCP result
```

Sources:

- `src/contract.py:84–90`: default mode is highlights, default result count is 10,
  maximum count is 20.
- `src/retrieval.py:125–154`: discovery is awaited first; page acquisition is
  awaited before memory and finalization. Highlights are lexical passage
  selection, not a generated research answer. Summary mode alone invokes the
  additional summarizer phase.
- `src/discovery.py:61–66,105–120` and `src/process.py:30–67`: two CLI admissions,
  request-deadline-aware queueing, version check and command execution.
- `src/cache.py:138–174`: cache misses are concurrent tasks, with a TaskGroup
  barrier; ordinary cache hits bypass browser admission. Freshness age defaults
  to one hour. Concurrent misses can acquire the same URL twice; they are not
  coalesced by a per-URL cache lock.
- `src/acquisition.py:155–201`: origin-aware browser admission. The slot remains
  occupied through document completion, including PDF parsing, not just the
  initial HTTP response. Other clients share these slots/origin exclusion.

### 1. All-results barrier and per-origin serialization — demonstrated

The acquisition barrier does not return as soon as enough good evidence exists.
It waits for the slowest page or for queued pages in an origin to finish/expire.
Increasing global concurrency alone cannot parallelize ten pages from one origin.

With similar page times `t`, the browser part is approximately bounded below by:

```text
max(total page work / global concurrency,
    largest sum of page work for one origin,
    slowest individual page)
```

This is a scheduling explanation, not a precise formula for production wall time:
startup, discovery, other clients, synchronous work and cleanup also matter.

**Budgeting happens after acquisition.** A small final response does not imply
that little work was done. In the isolated 20-page probe below, all 20 pages were
acquired even though the default 12 KiB response budget returned only 11.
See `src/retrieval.py:59–89,139–154` and `src/budget.py:30`.

### 2. Slow navigation/challenges can consume most of 300 seconds — verified allowance

`page.goto(..., wait_until='domcontentloaded', timeout=0)` and later
DOMContentLoaded checks have no independent native timeout
(`src/browser.py:144,245`). The enclosing acquisition uses the request deadline
(`src/browser.py:83–102`); the default is 300 seconds
(`src/acquisition_policy.py:28`, `src/server.py:58`).

Challenge continuation repeatedly checks the native document until it resolves
or the request expires (`src/browser.py:126–161`). There is no short
"give up on this source and return the others" threshold. This permits a single
180-second navigation/continuation to dominate a whole otherwise-fast search.
It does not establish that the logged production requests hit challenges.

### 3. Settling and expansion add page work, but are not a fixed minutes-long wait

Rendered-DOM settling checks for 400 ms of quiet, with a three-second cap per
settling attempt (`src/browser.py:164–211`). Network-idle observation is concurrent
and informational, not a mandatory long wait.

Expansion has a ten-second default cap and waits for post-action quiet/native
loads (`src/content_expansion.py:151–223`; `src/acquisition_policy.py:36–41`).
Scrollable pages may get a test scroll even without an expandable control.
A no-progress page ordinarily exits early; the full ten seconds are not always
spent. Document replacement can trigger further validation/render attempts
under the original request deadline.

For illustration, ten same-origin pages each spending three seconds settling
and ten seconds expanding would already accumulate about 130 seconds, before
navigation and other work. With ten independent origins and no contention, that
same artificial workload would need roughly three waves, about 39 seconds.
These are **capacity examples**, not measurements or hard upper bounds.

### 4. Request deadlines are not strict end-to-end wall-clock limits — demonstrated

Cancellation must drain owned resources. `Browser._acquire` shields page cleanup
and awaits `page.close()` without a separate cleanup timeout
(`src/browser.py:354–369`). A timing probe with a 30 ms acquisition deadline and
180 ms simulated page close returned `fetch_timeout` after **210.8 ms**.

Other potentially non-preemptible work includes:

- Synchronous extraction on the event loop (`src/browser.py:320–324`).
- Cache directory scans, JSON reads/writes and `fsync` on the event loop
  (`src/cache.py:41–113,152–165`).
- Synchronous highlight selection/redaction/budgeting (`src/retrieval.py:59–89`).
- Memory queries nominally get at most five seconds, but cancellation drains an
  already-running SQLite offload (`src/retrieval.py:147–153`,
  `src/memory.py:296–304,322–330`).

These are possible extensions/contention sources, **not measured causes of the
production minutes**. Do not remove resource ownership/draining simply to make
a deadline look strict; any cleanup change needs its own safe bounded lifecycle.

## Measurements

### Historical real-use searches

The Pi audit excluded this repository's development/evaluation sessions and
included direct and Code Mode calls, deduplicating copied records.

| Recorded mode | Calls | Tool-level errors | Successful calls with timing | Median | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| discovery | 27 | 0 | 25 | 3.683 s | 13.455 s |
| highlights/default | 57 | 7 | 50 | 13.562 s | 52.209 s |

All seven recorded search-call errors in this population were client timeouts
in highlights mode. Two successful discovery calls did not preserve duration.
"Successful" here is transport/tool-level and can include failed sources.
Queries, cache states, concurrency and deployment versions are not controlled;
this is association, not a paired causal benchmark. Timeouts censor the slow
highlight tail, so the successful-call median understates user-visible pain.

Trace examples relative to `~/.pi/agent/sessions/`:

- Slow highlights: `--home-dzervas-Lab-cadara--/2026-10-17T04-27-39-675Z_01a1481d-911a-71aa-b034-9d5414377d2c.jsonl:20`
  — three requested results, 52.209 seconds, with Docker source timeout/queue errors.
- Timeout then discovery fallback:
  `--home-dzervas-Lab-homelab--/2026-10-19T23-11-46-278Z_01a1566f-7065-7028-8dc4-0e8aebef7536.jsonl:14–20`.

### Current-code isolated probes

The probes used real `Retrieval`, `CachedAcquisition`, `DocumentCache` and
`Acquisition`, replacing only upstream discovery/native page work with controlled
async delays. Every scenario used a temporary cache; memory was disabled.
No Chrome/network/Kagi request was made. Timings are single local samples, not
production performance targets.

| Scenario | Native page delay | Elapsed | Peak active | Native visits |
| --- | --- | ---: | ---: | ---: |
| 10 different origins | 80 ms each | 262.4 ms | 4 | 10 |
| 10 pages, same origin | 80 ms each | 814.3 ms | 1 | 10 |
| 20 pages, same origin | 80 ms each | 1657.9 ms | 1 | 20 |
| 10 different origins, one slow page | nine at 10 ms; one at 400 ms | 429.4 ms | 4 | 10 |
| discovery-only | acquisition bypassed | 0.2 ms | 0 | 0 |
| ordinary, cache already populated | acquisition bypassed | 8.8 ms | 0 | 0 |

The separate cleanup probe used the real `Browser.acquire/_acquire` cancellation
path with a fake native page: 30 ms deadline + 180 ms cleanup -> 210.8 ms total.

The temporary probe command was `.venv/bin/python
/tmp/vresto-search-latency-probe.py`, with the sandbox's C++ runtime directory
supplied in `LD_LIBRARY_PATH`. The scratch probe was removed after the
investigation; it is not a shipped benchmark.
Permanent regression tests covering the core mechanisms include:

- `tests/test_acquisition_concurrency.py::test_parallel_limit_origin_waiters_do_not_hold_slots_and_order`
- `tests/test_acquisition_concurrency.py::test_cached_hit_bypasses_full_admission_and_misses_share_deadline`
- `tests/test_deadline.py::test_discovery_and_acquisition_share_deadline`
- `tests/test_deadline.py::test_more_than_old_queue_capacity_eventually_succeeds`
- `tests/test_deadline.py::test_version_check_and_command_do_not_reset_deadline`

## Next discriminating measurement, before changing runtime behavior

1. Identify the deployed image revision/settings and obtain the corresponding
   client call arguments, preferably from the missing OpenCode history.
2. Capture privacy-safe spans for discovery queue/execution, browser queue,
   navigation/challenge, settling, expansion, extraction/cache I/O, memory,
   selection and cleanup. Correlate by an opaque request ID/source index; never
   log queries, credentials, bodies or raw exception strings. Distinguish
   overlapping inclusive spans from the request critical path; do not sum them.
3. Run the same query/result count as discovery-only, ordinary highlights and
   fresh-only highlights with Chrome already warm. Record cache states and
   origin distribution. This separates upstream search, browsing and caching
   without attributing unrelated queries' timing differences to one stage.
4. If source waiting dominates, choose an explicit interactive response budget
   and partial-result policy while preserving canonical cache/evidence semantics.
   Do not assume raising global concurrency fixes same-origin serialization.
   Shortening shared deadlines or abandoning slow pages trades coverage for
   latency and needs an intentional product decision.
5. If discovery dominates, investigate its queue and pinned CLI execution. If
   local work or cleanup dominates, profile that measured boundary first.

No live paid upstream experiment, runtime fix or deployment change was performed.

## Verification and environment gaps

- Targeted scheduling/deadline checks: **9 passed, 26 deselected** in 1.46 seconds.
- The isolated timing probes and cleanup assertions passed.
- `uv run --locked basedpyright` and `uv run --locked ruff check .` could not
  spawn their executables (`No such file or directory`).
- A full-suite attempt through `uv run --locked python -m pytest -q --maxfail=1`
  stopped at its first native GitHub fixture test: browser startup could not
  execute Patchright's bundled Node. The remainder of the suite is unverified.
- The direct `pytest` launcher also could not spawn; module invocation worked
  with the sandbox C++ runtime library path. No environment repair was attempted.
