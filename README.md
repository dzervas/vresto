# Kagi Crawler

Private CPU-only Streamable HTTP MCP at `/mcp`, with **search** and **fetch**.
Kagi discovers sources; Crawl4AI/Patchright acquires public pages; lexical BM25
selects verbatim, version-bound evidence. No GPU, models, external database,
external browser service or paid browser license is required.

> Subscriber automation is an **unsupported, unofficial HTML interface**, not the
> Kagi API. Markup/session changes or upstream restrictions can break discovery;
> account access may be affected. Confirm authorization and Kagi's current terms.
> There is no automatic paid API fallback or unattended-availability guarantee.

## Build and run

The final image includes Microck Kagi CLI 0.22.0 at
`cb64cf514a99e527429468f92bc9f6b633a765e4`, Crawl4AI 0.9.4, Patchright/Playwright
1.63.0 and Patchright's exact Chromium **153.0.8010.12 / revision 1243**.
Dependencies are locked in `uv.lock`. Base/tool image tags are version-pinned,
not digest-reproducible; resolve approved digests in your release process and
retest coordinated driver/browser upgrades rather than freezing security updates.

```sh
# The streamed allowlist is additionally protected by .dockerignore.
# Neither command reads .envrc, .beads, .jj or credential files.
set -o pipefail
python3 scripts/build_context.py | docker build -t kagi-crawler:local -
python3 scripts/container_check.py kagi-crawler:local
```

Provision two **different** runtime secrets using your secret manager:
`KAGI_CRAWLER_ACCESS_TOKEN` (private MCP bearer access) and `KAGI_SESSION_TOKEN`
(subscriber discovery). No credentials are needed during build. Never put them in
Dockerfile, build args, image tags, source, URLs or client tool arguments. The
historical `KAGI_SESSION_TOKNE` spelling is not a supported variable. CLI auth
files are not read and API-token auth is not a fallback.

With those two variables already provisioned in the operator's environment:

```sh
docker volume create kagi-cache
docker run --name kagi-crawler --init --restart unless-stopped \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 3g --cpus 2 --pids-limit 256 --shm-size 256m \
  -p 127.0.0.1:8000:8000 \
  --mount type=volume,src=kagi-cache,dst=/data/cache \
  --env KAGI_CRAWLER_ACCESS_TOKEN --env KAGI_SESSION_TOKEN \
  kagi-crawler:local
```

These cgroup limits are **provisional containment**, not measured deployment
requirements or a promise that every page/PDF fits. Repeat the release gate on
deployment hardware before choosing production CPU/RAM limits. Runtime UID/GID
is **10001:10001**. Named-volume initialization inherits the image cache ownership;
a bind-mounted cache must already be writable by that UID (ask the host operator
to prepare it). Keep this directory private: it contains acquired documents and
version history. Home/temp space must be writable for transient browser/parser
work. Do not mount a private browser profile or CLI credential directory.

Native installation: Python 3.12, `uv sync --locked --python 3.12`,
`uv run python scripts/install_kagi.py` (Cargo), then
`uv run python -m patchright install chromium` with OS libraries already present.
Add `.tools/bin` to PATH. No daemon, root privilege or system-library installation
is performed by the service. Native bind defaults to `127.0.0.1`; image bind is
`0.0.0.0`. Override `KAGI_CRAWLER_HOST` / `KAGI_CRAWLER_PORT` or `--host` / `--port`.

## Private access and safety

Clients send `Authorization: Bearer <service-access-token>`; never send the Kagi
session token. All routes, including `/credits`, require service access. Keep the
port loopback/private and terminate TLS at a **trusted private edge** when crossing
hosts. Restrict edge access, redact Authorization/query/body logging, preserve MCP
streaming and cancellation, and allow bounded requests long enough to finish.
There is no OAuth onboarding, public tenancy or per-client authorization.
Runtime environment secrets remain visible to container administrators; use a
trusted daemon/host, rotate tokens after exposure and never dump container env.

Discovery credentials are confined to the discovery subprocess; browser/driver
and PDF workers receive allowlisted environments. Acquisition rejects non-HTTP(S),
credentials in URLs, nonstandard public ports, private/loopback/link-local/metadata
destinations, redirects, DNS rebinding and forbidden browser subrequests. Fixture
exceptions are Python harness-only, not production flags or MCP controls. Use
host-level outbound filtering and process isolation as defense in depth: this is
not an engine-exploit sandbox. Fetched pages are **untrusted data**, never agent
instructions. Do not enable private authenticated browsing or arbitrary JS/CDP.

Stealth is best effort: traffic goes through bounded stateless aiohttp transport,
not Chromium's HTTP fingerprint. No cookies/caller headers/private sessions are
forwarded; GET/HEAD only. Cookie-dependent sites, POST APIs and redirects during
rendering can fail. Challenges are reported conservatively, not bypassed. No
CAPTCHA solving, proxy rotation, paywall bypass or guaranteed anti-bot evasion.

## Tools, freshness and limits

Discover exact input schemas with MCP `tools/list`. Inputs use snake_case.
Examples (tool arguments, not HTTP JSON-RPC envelopes):

```json
{"query":"retry_limit version 2","num_results":10}
{"query":"Python reference","content_mode":"discovery"}
{"urls":["https://docs.python.org/3/library/asyncio.html"],"query":"cancellation"}
{"urls":["https://docs.python.org/3/library/asyncio.html"],"freshness":"fresh_only","content_mode":"full_text"}
{"evidence_ids":["<returned-version-bound-evidence-id>"],"response_budget_bytes":65536}
```

Search defaults to acquired highlights, not provider snippets. Result counts are
limits, not guarantees. Supported subscriber controls include region/lens,
time window or explicit dates, order, verbatim and personalization; unsupported
controls are rejected. Provider dates mean **update dates**, not verified
publication dates. Postfilters, per-source errors, budget omissions and partial
coverage are disclosed; snippets never silently replace failed acquisitions.
Lexical matches are not answer verification and absence of a highlight does not
prove absence of an answer. Full text, expansion and original sources support review.

| Bound | Default | Hard limit / configuration |
|---|---:|---|
| Search result count | 10 | MCP `num_results`: 1–20 |
| Direct URLs / evidence IDs | 1–10 | 10; mutually exclusive forms |
| Serialized response bytes | 65,536 | 4,096–262,144; MCP `response_budget_bytes` or `KAGI_CRAWLER_RESPONSE_BUDGET_BYTES` |
| Acquisition request deadline | 30 s | `KAGI_CRAWLER_DEADLINE_SECONDS`: 0.01–120 s, one deadline across composed pages |
| Browser concurrency | 1 | Fixed serialized acquisition across clients |
| Waiting queue | 8 | `KAGI_CRAWLER_QUEUE_CAPACITY`: 1–32; excess is `queue_full` |
| Queue wait | 10 s | `KAGI_CRAWLER_QUEUE_WAIT_SECONDS`: 0.01–30 s; `queue_timeout` or request timeout |
| Browser work-idle teardown | 1 hour | Fixed; never interrupts active acquisition; any MCP request warms an absent browser |
| Upstream read timeout | 10 s | Fixed per HTTP operation, also request deadline |
| Decoded response / page job | 2 MiB / 8 MiB | Fixed aggregate body containment, 64 requests/page, 5 redirects, 32 KiB headers |
| PDF parse | 100 pages / 10 s | Fixed isolated parser, 1.5 GiB worker address-space ceiling, also acquisition deadline |
| Persistent canonical cache | 256 MiB / 7 days | `KAGI_CRAWLER_CACHE_CAP_BYTES`: 1 KiB–1 GiB; `KAGI_CRAWLER_CACHE_RETENTION_SECONDS`: 0.01 s–30 days |
| Ordinary freshness | 1 hour | MCP `max_age_seconds`: 0–30 days |
| Opt-in traversal | 5 pages / depth 1 / 15 s | MCP `traversal`: pages 1–20, depth 0–3, time >0–30 s, frontier 1–128 (default 32), aggregate bytes 1 KiB–32 MiB (default 8 MiB) |

Traversal requires one URL plus explicit exact `origin` and `path_prefix`; no
subdomain escape, unrestricted fanout or exhaustive-crawl promise. Attempt/page
counts include cache hits and failures. Every page still uses queue, destination,
byte and shared deadline controls. No immediate acquisition retries: rate-limited
origins back off for five seconds; later requests may try again.

The byte budget includes the text and structuredContent copies plus substantive
metadata, **not tokens**, HTTP headers or the JSON-RPC envelope. Highlights retain
whole qualified passages; full text can be explicitly truncated. Output bounds
were fixture-measured beside `budget.py`; queue/time/byte limits are conservative
containment, not inferred Exa parity. Cache sizing measurements are beside
`server.py`. Serialized cache bytes exclude filesystem overhead.

`ordinary` uses sufficiently recent cache and permits labeled stale fallback when
refresh fails; `refresh` always acquires but allows that fallback; `fresh_only`
always acquires and **never** returns stale fallback; `cache_only` never acquires.
Responses identify fetch time, content version and cache state; fallback includes
original age and refresh failure. Different queries reuse canonical documents,
not query-dependent selections. Evidence expansion refers to the **exact retained
version**, survives cache-mounted restarts, and never substitutes current text.
Oldest fetched versions expire/evict within cache bounds (reads do not renew them);
evicted evidence returns `evidence_unavailable`. This is not a permanent archive.
PDF support is basic text parsing with page references, not OCR or robust
scientific/layout reconstruction. Scanned/unsupported files fail honestly.

## Verification, measurement and diagnostics

```sh
uv run --python 3.12 pytest -q
# Use a NEW empty cache for each cold measurement; fixed loopback port 18081 must be free.
uv run --python 3.12 python scripts/release_check.py --cache /tmp/kagi-release-cache
# Separate process, same mounted cache: verify cached text/evidence without page requests.
uv run --python 3.12 python scripts/release_check.py --cache /tmp/kagi-release-cache --verify-persisted
python3 scripts/container_check.py kagi-crawler:local
```

The real TCP MCP harness covers composed search/fetch, rendered technical content,
access rejection, warm/cache latency, serialized multi-client queue pressure,
accelerated one-hour teardown, same-session recreation, external browser crash,
process cleanup and cache/evidence persistence in a fresh process/container.
Raw native facts and methodology live **beside `scripts/release_check.py`**.
RSS sums the Python process tree (shared pages double-counted), sampled every
20 ms; short peaks can be missed. Cold latency includes MCP initialization and
cold browser acquisition, not Python imports/image pull. Fixtures are tiny and
not representative heavy-site/PDF sizing. Deployment must repeat CPU/RSS/cgroup
peak, idle/active, queue saturation and public-site latency measurements.

Normal tool diagnostics contain only tool name, elapsed time and outcome; browser
lifecycle/queue events are fixed labels. HTTP access logging is off; MCP responses
provide cache/error/limit details. Do not turn on debug logging or log request,
response, environment or page bodies. For incidents inspect sanitized error
codes, elapsed times, process counts and cache usage; investigate upstream auth
through the operator, never by returning tokens or dumping CLI stderr.

**Unverified release gates:** Docker currently cannot connect to
`/run/user/1000/docker.sock` (socket absent). No image build/pull, Rust image
compilation, Debian runtime launch, Docker UID/volume initialization, seccomp,
cgroup resource sizing or container SIGTERM test has been claimed. The runnable
container harness is provided for an operator with a reachable daemon; do not
start a daemon or request root just to bypass this gate.

No authorized live Kagi session or Exa gateway comparison was used. Prior-ticket
Exa **source/tool-shape** comparisons are not live acquisition-quality evidence.
For authorized evaluation, curate JSON cases with `category`, `url`, `query`, and
literal `expected` strings across technical docs, identifiers, qualifications,
tables, JavaScript, text PDFs and absent-answer cases, then explicitly opt in:

```sh
# Uses only the private service bearer provisioned by the operator.
uv run --python 3.12 python scripts/live_smoke.py --authorized-live \
  --endpoint https://your-private-edge/mcp --cases /path/to/public-cases.json --budget 65536
```

Repeat against authorized Exa under comparable evidence budgets, manually review
citations/negation/table fidelity, and normalize boolean acquisition/selection
observations before optional `--exa-observations`. Separate **acquisition failure**
from expected text acquired-but-not-selected; truncated canonical output cannot
establish completeness. Neither script judges answer correctness or matches
rankings. Live acquisition/challenge rates, PDF citation quality and Exa selection
quality comparisons remain missing; no paid credentials or calls are required
for deterministic checks. Embeddings, rerankers, extraction and corpus search
remain outside the baseline dependency closure.

## Attribution

This product includes software developed by UncleCode (https://x.com/unclecode)
as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).

Keep `NOTICE`, `licenses/Crawl4AI-LICENSE` (including its appended attribution
requirement), packaged Kagi/Rust notices and installed dependency/driver/Chromium
third-party notices when redistributing. The image retains them; attribution is
also available through authenticated `/credits`. This repository does not
change upstream licenses.
