# Working on vresto

## Module map

Paths below are relative to `src/`.

- `retrieval.py`: search/fetch workflow, evidence selection, redaction and final budget; `Retrieval.search(SearchInput)` / `.fetch(FetchInput)` / `.expand(ExpandInput)` / `.crawl(CrawlInput)` return tool data; internal `SearchRequest`/`FetchRequest` retain operational controls.
- `acquisition.py`: browser lifecycle, bounded origin-aware concurrent queue and shared deadline; `Acquisition.fetch_many(urls, scope=...)`, request/warmup/close lifecycle.
- `acquisition_policy.py`: acquisition limits, literal URL screening and classified failures; `AcquisitionOptions`, `DestinationPolicy.url`, `FetchError` (not network isolation).
- `browser.py`: native navigation, challenge continuation, render settling and serialization; `BrowserAdapter` exposes start/acquire/close/is_connected, returning `BrowserResult`.
- `chromium.py`: Chrome-only bootstrap, DoH preferences/policy verification and owned driver/profile cleanup; `Chromium.start/close` supplies the native browser.
- `content_expansion.py`: bounded read-only in-page controls, scrolling and deduplication; `expand_content` takes adapter selectors/hooks and returns coverage.
- `normalization.py`: generic semantic HTML cleanup and document boundaries; `main_content_html(document)` returns cleaned HTML.
- `extraction.py`: pure rendered HTML → canonical Markdown and original link targets; `extract(document, url) -> Extracted` is the only HTML conversion path.
- `sites/`: fragile site markup, ordered adapters and fail-soft orchestration; `expand_content` / `extract_document` disclose adapter coverage. Before adding/fixing an adapter, read the guide in `sites/__init__.py`'s docstring.
- `cache.py`: versioned canonical documents, retention/eviction and freshness/stale rules; `DocumentCache.lookup/store/usage`, `CachedAcquisition.fetch_many(..., scope=...)`.
- `traversal.py`: bounded breadth-first crawling with explicit document scope; `Traversal.run(seed)` returns acquisitions plus traversal metadata.
- `evidence.py`: BM25 passage selection, version-bound evidence IDs and exact retained-version expansion; `select_highlights`, `evidence_id`, `resolve_evidence`, `expand_evidence` (cache first, exact memory version fallback).
- `memory.py` / `memory_worker.py`: bounded background passage ingestion, independent SQLite/FTS5 history and hybrid RRF search; `Memory.submit/query/close`, `MemoryStore.lookup`; isolated ONNX worker owns model inference. Model pins/checksums live in `scripts/install_embedding_model.py`; measure retrieval with `scripts/evaluation/run.py memory`.
- `budget.py`: complete MCP result serialization and byte limits; `result`, `wire_bytes`, `apply_budget` trim responses, never canonical cache text.
- `discovery.py` / `process.py`: pinned Kagi CLI JSON contract and bounded subprocess/process-group ownership; `Discovery.search`, `DiscoveryError`, `Processes.admission/run/close`.
- `summaries.py`: generated Kagi Summarizer summaries of canonical Markdown (HTML output converted to Markdown), cached per content version, failing per source; `add_summaries`. Generated text, never evidence.
- `research.py`: `research` tool over Kagi Assistant (small = default model, big = configured model) with its own process pool, deadline, daily caps and thread deletion; optional citation verification through fetch/highlights; `Research`.
- `pdf.py` / `pdf_worker.py`: bounded PDF parsing in an isolated worker; `parse_pdf` returns text/title/page metadata; worker takes a service-owned file path and emits bounded JSON on stdout.
- `types.py`: shared result/coverage TypedDicts and `ErrorCode`; public wire shapes and private acquisition metadata are explicit.
- `server.py`: settings, MCP/HTTP validation, identity logging and lifespan; `create_app(Settings)` / CLI `main`; public `GET /health` is minimal liveness, authenticated requests disclose coarse state/counters without starting Chrome.
- `auth.py`: SDK TokenVerifier and scope challenges, issuer discovery/JWKS, JWT/introspection and explicit weaker opaque/userinfo opt-in; OAuth resource server only. Auth changes → `tests/test_auth.py` (local fake AS, no browser).
- `contract.py`: validated public search/fetch/expand/crawl inputs and internal request controls; keep advanced freshness/traversal tests at the Retrieval/cache seam, not in MCP schemas.
- `safety.py`: credential detection/redaction helpers used at output and cache boundaries.
- `native_observation.py`: pending native fetch/XHR tracking without interception; `NativeLoadObservation(page)` / `.close()` owns page listeners.
- `headful.py`: private Xvfb lifecycle and service launcher; CLI `main` owns display cleanup.
- `browser_check.py`: standalone browser runtime diagnostic; CLI `main` reports effective browser settings.

## Where to change behaviour

- Site markup fix → `sites/<site>.py` + `tests/sites/<site>/` only; keep fragile selectors/assumptions there, with regression fixtures.
- Generic extraction → `extraction.py` / `normalization.py`; run `UPDATE_GOLDEN=1 uv run --locked pytest tests/test_extraction_golden.py` and review every Markdown diff.
- New limit → its owner (`AcquisitionOptions`, `TraversalInput`, `Processes`, or `Settings`); update validation, public schema/disclosure where applicable, and boundary tests. Response byte bounds live in `budget.py`.
- New error code → `types.ErrorCode`, then the producing boundary and its response tests; keep fixed classifications rather than exception text.
- Kagi CLI upgrade → coordinate the `discovery.py` version pin, `Dockerfile` and `scripts/install_kagi.py`; verify the CLI JSON/flags against discovery tests.

## Checks and fixtures

Use Python 3.12 and `uv sync --locked`. Native browser tests need Patchright's browser/runtime libraries and a usable Chrome installation (`uv run --locked python -m patchright install chromium` provisions the bundled browser, not OS libraries).

```sh
uv run --locked basedpyright
uv run --locked ruff check .
uv run --locked pytest
```

The full suite takes about 10 minutes and launches real browsers. Run all three checks before handing off; report exact results and anything unverified.

Capture public rendered DOM (URL and output basename, without suffix):

```sh
uv run --locked python scripts/capture_fixture.py URL tests/sites/<site>/NAME --settle 3
# Challenge-protected sites usually need headed real Chrome:
uv run --locked python scripts/capture_fixture.py URL tests/sites/<site>/NAME --headed --channel chrome --settle 8
```

Headed capture needs `DISPLAY` pointing to a working X server (e.g. a private Xvfb); unset `WAYLAND_DISPLAY` if Chrome picks Wayland. Use `--executable /path/to/chrome` instead of `--channel` for an explicit installation. Read the adapter's refresh instructions, review captured HTML for sensitive data, regenerate its goldens as documented there, and review the Markdown diff.

## Invariants

- Public, logged-out acquisition only: no credentials, supplied cookies, login or CAPTCHA solving in browser acquisition or fixture capture. Service/upstream credentials stay confined to their existing auth/discovery boundaries.
- Preserve MCP wire output unless the task explicitly changes the contract; diagnostics use fixed phases/reasons, never queries, credentials or raw exceptions. The only URLs logged are failed/stale sources (`source_failed`/`source_stale` in `retrieval.py`), after credential screening.
- Canonical text changes alter content versions and evidence offsets/IDs. Budgeting and query selection operate on response copies, not canonical cache records. Memory evidence resolves only exact retained versions; retained results disclose origin/date, never enter lexical highlight reselection, and lose budget priority to live results. `fresh_only` excludes memory.
- Fragile site-specific code belongs only under `sites/`; generic extraction/normalization must remain host-independent. Keep fallback and partial-coverage disclosure honest.
