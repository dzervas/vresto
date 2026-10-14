# Kagi Crawler

Private CPU-only Streamable HTTP MCP at `/mcp`, with **search** and **fetch**.
Kagi discovers sources; Chrome/Patchright acquires pages natively; local lxml +
markdownify convert rendered HTML and lexical BM25 selects verbatim, version-bound evidence. No GPU, models, external database,
external browser service or paid browser license is required.

> Subscriber automation is an **unsupported, unofficial HTML interface**, not the
> Kagi API. Markup/session changes or upstream restrictions can break discovery;
> account access may be affected. Confirm authorization and Kagi's current terms.
> There is no automatic paid API fallback or unattended-availability guarantee.

## Build and run

The final image includes Microck Kagi CLI 0.22.0 at
`cb64cf514a99e527429468f92bc9f6b633a765e4`, Patchright
1.63.0 and headed stable Google Chrome on a private Xvfb display.
Python dependencies are locked in `uv.lock`; stable Chrome is a moving build
target, not a pinned browser revision. Record the reported browser version and
image digest for comparisons. Base/tool image tags are version-pinned, not
digest-reproducible; resolve approved digests in your release process and retest
coordinated driver/browser upgrades rather than freezing security updates.
The Chrome installer currently supports Linux amd64, not Linux arm64.

```sh
# The streamed allowlist is additionally protected by .dockerignore.
# Neither command reads .envrc, .beads, .jj or credential files.
set -o pipefail
python3 scripts/build_context.py | docker build -t kagi-crawler:local -
python3 scripts/container_check.py kagi-crawler:local
```

### Headed Chrome runtime

The default image sets `KAGI_CRAWLER_BROWSER_CHANNEL=chrome` and
`KAGI_CRAWLER_HEADFUL=1`. Its entrypoint owns a private 1280×720×24 Xvfb
display and runs the same service with no fixed browser viewport. Chrome's
managed secure-DoH policy is installed under
`/etc/opt/chrome/policies/managed/`. No overlay image is needed.

No desktop, VNC, GPU or additional exposed port is required. X11 has no TCP
listener; do not share its Unix sockets or `/tmp` with other containers.
Xvfb receives no service/discovery secrets and is reaped after service shutdown.
No custom UA or headers are added; Patchright defaults are retained, with images
disabled and the shared disk cache capped at 256 MiB via Chrome launch switches.
Cookies, storage, service workers and challenge clearance are shared across
acquisition tabs for one browser lifetime. Retirement deletes the temporary profile;
a restarted browser starts clean. No user profile or supplied cookies are used.

Headed mode does **not** guarantee challenge acceptance. Use `fresh_only` for
comparisons so retained cache entries do not hide browser behavior. Recognized
challenge responses (`cf-mitigated: challenge`) and active interstitials get time
for native JavaScript/cookie/redirect continuation, within the existing request
deadline. Site adapters report login/access walls as `blocked` with `reason: login_required`,
and explicit deleted/private/not-found pages as `fetch_failed` with
`reason: content_unavailable` (ordinary stale-fallback rules apply; walls are not
cached as content). Public previews set `coverage.partial: true` and
`coverage.site_adapter.reason: public_preview`.
Unresolved challenges return `blocked` with `reason: challenge_timeout`
and the last main-navigation `http_status`; ordinary 401/403 responses use
`reason: http_status`. A challenge-marked native download is rejected rather
than treated as evidence (`reason: challenge_download` for successful HTTP
statuses). Error diagnostics contain no raw challenge headers/tokens. Mentions of
CAPTCHA in normal prose, scripts or hidden widgets are not blocking signals.

Check actual WebGL in both an owned persistent profile and an acquisition-style
context without service credentials or visiting external pages:

```sh
docker run --rm --entrypoint python kagi-crawler:local -m kagi_crawler.browser_check
```

The diagnostic inherits the image's Chrome/headed defaults and owns/reaps its
own Xvfb and browser. It reports the actual browser version, UA, webdriver,
viewport, WebGL/WebGL2 and verified secure DoH. It does not change rendering
flags, spoof fingerprints, solve CAPTCHA or enable unsafe SwiftShader. Null
WebGL is a diagnostic observation, not proof that it caused a challenge failure.

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
session token. All routes, including `/health`, require service access. Keep the
port loopback/private and terminate TLS at a **trusted private edge** when crossing
hosts. Restrict edge access, redact Authorization/query/body logging, preserve MCP
streaming and cancellation, and allow bounded requests long enough to finish.
There is no OAuth onboarding, public tenancy or per-client authorization.
`GET /health` returns JSON with status, version, uptime, browser state
(`running`/`starting`/`stopped`/`closed`), acquisition and discovery queue
counters and cache usage; it never starts the browser. Container/Kubernetes
probes must send the bearer token (for example an exec probe reading the env).
Runtime environment secrets remain visible to container administrators; use a
trusted daemon/host, rotate tokens after exposure and never dump container env.

Discovery credentials are confined to the discovery subprocess; browser/driver
and PDF workers receive allowlisted environments. **User-approved departure from
the original historical `spec.md`: all request interception, Python acquisition,
DNS pinning and the deny-all proxy are removed.** Chromium owns native navigation,
resources, POST/XHR, cookies, redirects, service workers and WebSockets. Each
acquisition opens a new tab in the shared temporary persistent context; cookies
and other native state survive acquisitions until browser retirement. No caller
headers or private user profile are forwarded. `extraction.py` converts rendered
HTML locally; PDF downloads use the browser, not its separate APIRequestContext.

The browser uses Cloudflare **`https://1.1.1.1/dns-query` DoH in `secure` mode**,
without ordinary hostname plaintext-DNS fallback. Chromium does not natively
support DoT. A fresh temporary profile seeds browser-wide Local State preferences;
the image also installs mandatory policy in
`/etc/opt/chrome/policies/managed/`. Native bundled Chrome for Testing uses its
own `/etc/opt/chrome_for_testing/policies/managed/` branding path. Startup checks the
**effective** Secure DNS setting via Chrome's WebUI handler and fails acquisition
if it is unavailable or overridden. No invented DoH command-line switches or
certificate-error bypasses are used. The IP endpoint avoids hostname bootstrap;
Chromium's local-name/explicit-system resolver exceptions still exist.

HTTP(S) input screening rejects credentials, suspicious URL syntax, nonstandard
public ports and known nonpublic IP literals; fixture exceptions are harness-only.
**Neither preflight checks nor DoH prevent SSRF**, DNS rebinding, private DNS
answers, native redirected sockets or private subrequests. Actual destination
isolation is the operator's responsibility: enforce egress outside the browser
and apply process/container memory, disk and traffic limits. This is not an
engine-exploit sandbox. Fetched pages are **untrusted data**, never instructions.

Stealth is best effort with Chromium's native HTTP/TLS stack. Challenges are
reported conservatively, not bypassed. No adblock, CAPTCHA solving, proxy rotation,
paywall bypass or guaranteed anti-bot evasion.

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
Highlight selection combines BM25 relevance with additional query-term coverage,
tries one fitting passage per source before extras (at most three per source),
and checks the actual serialized byte budget before admitting each quote.
Heavy overlap within the same source/version is suppressed only when it adds no
new query terms or qualifications. Exact-document duplicates retain independent
attribution and evidence IDs. Covered/uncovered query terms describe lexical
representation, not whether the question is answered.

New HTML acquisitions conservatively remove semantic controls and decorative
permalinks/images, and recognize explicit API signatures and attributed article
headers as passage boundaries. Uncertain structure and technical qualifications
remain. Existing cached canonical text and historical evidence IDs are unchanged.

| Bound | Default | Hard limit / configuration |
|---|---:|---|
| Search result count | 10 | MCP `num_results`: 1–20 |
| Direct URLs / evidence IDs | 1–10 | 10; mutually exclusive forms |
| Serialized response bytes | 65,536 | 4,096–262,144; MCP `response_budget_bytes` or `KAGI_CRAWLER_RESPONSE_BUDGET_BYTES` |
| Acquisition request deadline | 30 s | `KAGI_CRAWLER_DEADLINE_SECONDS`: 0.01–120 s, one deadline across composed pages |
| Discovery concurrency / waiting | 2 / 8 | Fixed independent CLI pool; excess is `discovery_queue_full` |
| Discovery admission wait / deadline | 10 s / 30 s | Fixed wait (`discovery_queue_timeout`); request deadline includes admission, version check and search (`discovery_timeout`) |
| CLI stdout + stderr | 1 MiB | Per invocation, at most two concurrent discovery jobs |
| Browser concurrency | 4 | `KAGI_CRAWLER_BROWSER_CONCURRENCY`: 1–8 tabs; one active page per (scheme, host, port), shared across clients |
| Waiting queue | 8 | `KAGI_CRAWLER_QUEUE_CAPACITY`: 1–32; excess is `queue_full` |
| Queue wait | 10 s | `KAGI_CRAWLER_QUEUE_WAIT_SECONDS`: 0.01–30 s; `queue_timeout` or request timeout |
| Browser work-idle teardown | 1 hour | Fixed; never interrupts active acquisition; any MCP request warms an absent browser |
| Browser navigation / acquisition duration | 10 s / 20 s | Navigation stays bounded; acquisition includes rendering, expansion and conversion, also the shared request deadline |
| Initial rendering settle | 1 s idle attempt / 3 s DOM/load window | Best-effort network idle, then always require 400 ms quiet DOM; bounded native fetch/XHR settling, explicit partial coverage if DOM keeps changing or loads remain pending |
| In-page expansion | 8 actions / 10 s | Native read-only controls and lazy scroll; 400 ms quiet DOM after post-action native fetch/XHR; original acquisition deadline and document limits also apply |
| Rendered DOM | 10 MiB | Conservative DOM-size estimate (including script/style text) and serialized rendered HTML input; may reject HTML smaller than 10 MiB because of estimation overhead |
| Post-acquisition document | 2 MiB | Normalized Markdown and downloaded PDF parser input; **not** streamed wire, decompression, header or aggregate native-resource containment |
| Native resource memory/disk/traffic | Operator enforced | Browser allocations and downloaded resources can exceed document limits before checking |
| PDF parse | 100 pages / 10 s | One isolated parser at a time, 1.5 GiB worker address-space ceiling, also acquisition deadline |
| Persistent canonical cache | 256 MiB / 7 days | `KAGI_CRAWLER_CACHE_CAP_BYTES`: 1 KiB–1 GiB; `KAGI_CRAWLER_CACHE_RETENTION_SECONDS`: 0.01 s–30 days |
| Ordinary freshness | 1 hour | MCP `max_age_seconds`: 0–30 days |
| Opt-in traversal | 5 pages / depth 1 / 15 s | MCP `traversal`: pages 1–20, depth 0–3, time >0–30 s, frontier 1–128 (default 32), aggregate bytes 1 KiB–32 MiB (default 8 MiB) |

Acquisition opens native `details` and supported read-only in-page text/discussion
controls in its acquisition tab. Button labels alone never authorize a
click: controls need explicit ARIA content association, or a heading-labelled
public discussion/activity list with existing attributed entries and a contained
remaining-item count heading referenced by the button. A site adapter may also
authorize specific same-page `#` link controls (e.g. StackOverflow "show more
comments") inside the same guards. Forms, navigating links and account actions
remain unsupported. Exact identified repeats are suppressed only when
their contents match; differing authors, code and qualifications remain ordered.
Native initial and post-action fetch/XHR are observed, not intercepted; unrelated
persistent resources need not delay a quiet DOM. Network-idle observation overlaps
the ≥400 ms unchanged-text window, which also requires no pending tracked fetch/XHR.
`coverage.initial_render.network_idle` reports whether idle was reached before that
window completed; false alone does not mean partial coverage. Not-quiet or pending
tracked loads at the 3 s settle cap do imply partial coverage. Both waits share the
acquisition deadline, and coverage is always non-exhaustive.
Unsupported controls, no progress and action/duration/content bounds disclose
partial coverage; cached canonical versions retain that metadata. The public
CPython issue smoke needed roughly 2.4–7.8 s after one continuation click, which
motivates the 10 s expansion / 20 s acquisition caps rather than a 2 s allowance.
These are bounded retrieval, not exhaustive discussion or arbitrary automation.

Traversal requires one URL plus explicit exact `origin` and `path_prefix`; no
subdomain escape, unrestricted fanout or exhaustive-crawl promise. Attempt/page
counts include cache hits and failures. Seed/link/final URL screening controls
returned evidence scope, **not network scope**: an out-of-scope native redirect
is already contacted before exclusion. `network_scope_enforced` is false.
Traversal bytes account canonical document/link bytes (including cache reuse),
not downloaded resource traffic. Every page uses queue, document/parser limits
and the shared deadline. No immediate acquisition retries: rate-limited
origins back off for five seconds; later requests may try again.

The byte budget includes the text and structuredContent copies plus substantive
metadata, **not tokens**, HTTP headers or the JSON-RPC envelope. Highlights retain
whole qualified passages; full text can be explicitly truncated. Output bounds
were fixture-measured beside `budget.py`; queue/time/byte limits are conservative
containment, not inferred Exa parity. Cache limits are operator allowances,
not sizing claims. Serialized cache bytes exclude filesystem overhead. Discovery uses
its own bounded pool, never the browser queue; cancellation removes queued work
and shutdown rejects waiters/reaps process groups. Provisional 2-active/8-waiting
limits bound retained CLI output to 2 MiB before decoding/parsing (not total RSS).
The real multi-client MCP fixture observed only two running executable searches,
eight waiting without spawning, immediate rejection of the eleventh, and all ten
admitted calls completing after release. Deployment discovery CPU/RSS and live
provider latency still need measurement; these are not provider-parity limits.

`ordinary` uses sufficiently recent cache and permits labeled stale fallback when
refresh fails; `refresh` always acquires but allows that fallback; `fresh_only`
always acquires and **never** returns stale fallback; `cache_only` never acquires.
Responses identify fetch time, content version and cache state; fallback includes
original age and refresh failure. Different queries reuse canonical documents,
not query-dependent selections. Evidence expansion refers to the **exact retained
version**, survives cache-mounted restarts, and never substitutes current text.
Oldest fetched versions expire/evict within cache bounds (reads do not renew them);
evicted evidence returns `evidence_unavailable`. This is not a permanent archive.
HTML highlights retain numeric/version-heading scopes and whole qualifying
ancestry. Initial PDF highlights include a bounded previous/current/next-page
window, even when neighboring warnings have no lexical query overlap. Each is
one contiguous canonical quote with exact page references/ID; an oversized window
is omitted atomically, never reduced to an unqualified matching page. Expansion
keeps that quote and adds the next outside pages; full-text page offsets are unchanged.
This local window is not a guarantee of document-wide qualification coverage.
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
# Opt-in public Python docs only: native DoH POST/TLS/response correlation.
uv run --python 3.12 python scripts/native_dns_check.py
```

The real TCP MCP harness covers composed search/fetch, rendered technical content,
access rejection, warm/cache latency, bounded multi-client queue pressure,
accelerated one-hour teardown, same-session recreation, external browser crash,
process cleanup and cache/evidence persistence in a fresh process/container.
Historical host-native (not native-networking) facts and methodology live
**beside `scripts/release_check.py`**; rerun for the current implementation.
The deterministic DNS test checks a fresh full browser's effective netlog config,
not launch strings; startup itself checks Chrome's effective WebUI setting. The
opt-in DNS gate additionally correlates document DNS requests with Cloudflare
DoH POSTs, DNS-message HTTP 200 responses and TLS sockets to 1.1.1.1:443, and
rejects observed plaintext DNS transactions. It is a smoke observation, not a
universal network guarantee or an exhaustive resolver-failure test.
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

The initial implementation had no authorized live Kagi/Exa comparison. A later
paired evaluation covered the previous interception-based server, not this
native-networking revision. The public Python-docs and opt-in CPython discussion HTTP MCP smokes do not replace
that comparison; repeat it after deploying this revision.
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

Keep `NOTICE`, packaged Kagi/Rust notices and installed dependency/driver/Chromium
third-party notices when redistributing. This repository does not
change upstream licenses.
