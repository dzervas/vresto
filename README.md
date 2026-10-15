# Kagi Crawler

Private CPU-only Streamable HTTP MCP at `/mcp`, with **search** and **fetch**.
Kagi discovers sources; Chrome/Patchright acquires pages natively; local lxml +
markdownify convert rendered HTML and lexical BM25 selects verbatim, version-bound evidence.
Optional persistent semantic memory adds dated retained passages to search using a
local CPU ONNX encoder and SQLite. No GPU, external database, external browser
service or paid browser license is required.

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
Chrome comes from Google's apt repository (google-chrome-stable) for linux/amd64 and
linux/arm64; pass `--build-arg TARGETARCH=amd64|arm64` when the builder does not
set it (BuildKit/buildx do; kaniko and the Woodpecker pipeline pass it explicitly).

```sh
# The streamed allowlist is additionally protected by .dockerignore.
# Neither command reads .envrc, .beads, .jj or credential files.
set -o pipefail
python3 scripts/build_context.py | docker build --build-arg TARGETARCH=amd64 -t kagi-crawler:local -
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

For static-token access, provision two **different** runtime secrets using your secret manager:
`KAGI_CRAWLER_ACCESS_TOKEN` (private MCP bearer access) and `KAGI_SESSION_TOKEN`
(subscriber discovery). OAuth can replace the static access token (see below).
No credentials are needed during build. Never put them in
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

Clients send `Authorization: Bearer <access-token>` (OAuth or static); never send
the Kagi session token. Configure OAuth below, a static `KAGI_CRAWLER_ACCESS_TOKEN`,
or explicitly set `KAGI_CRAWLER_UNAUTHENTICATED=1` (startup warning; no MCP auth).
Without any mode the service refuses to start. OAuth plus a static token accepts
either, trying OAuth first; the static token grants all configured scopes.
`KAGI_SESSION_TOKEN` remains required in every mode.

Keep the port loopback/private and terminate TLS at a **trusted private edge** when
crossing hosts. Restrict edge access, redact Authorization/query/body logging,
preserve MCP streaming and cancellation, and allow bounded requests long enough
to finish. This is a resource server, not an OAuth proxy or authorization server;
all authorized clients share the subscriber account/cache and required scopes.
`GET /health` without valid authentication returns only `{"status":"ok"}` for
liveness. With sufficient authenticated scope it returns version, uptime, browser
state (`running`/`starting`/`stopped`/`closed`), acquisition/discovery queues, cache
usage and memory counters; it never starts the browser or embedding worker.
Unauthenticated mode does not expose detailed health without credentials.
Runtime environment secrets remain visible to container administrators; use a
trusted daemon/host, rotate tokens after exposure and never dump container env.

### OAuth and pi

Set `KAGI_CRAWLER_OAUTH_ISSUER` to the authorization server's exact issuer and
`KAGI_CRAWLER_OAUTH_RESOURCE` to the **public URL of `/mcp`**, for example
`https://crawler.example.com/mcp`. In Kubernetes this must equal the public ingress
URL used by clients, not the service DNS name or internal HTTP URL. Publish
`/.well-known/oauth-protected-resource/mcp` through the same ingress (with any
resource path prefix); 401/403 challenges advertise that metadata URL. Optional
`KAGI_CRAWLER_OAUTH_SCOPES` is a space-separated list of required scopes.

Issuer discovery supports RFC 8414 and OIDC Discovery. RFC 9068 JWT access tokens
must have `typ: at+jwt` (or `application/at+jwt`), an asymmetric JWKS signature,
matching issuer/audience, and valid lifetime/required claims. ID tokens and HS256
are rejected. JWKS/discovery cache for five minutes; unknown signing keys trigger
a refresh at most once per 30 seconds. Opaque tokens use advertised RFC 7662
introspection with active/audience checks. For ASs requiring HTTP Basic resource
server authentication, provision `KAGI_CRAWLER_OAUTH_INTROSPECTION_CLIENT_ID` and
`KAGI_CRAWLER_OAUTH_INTROSPECTION_CLIENT_SECRET` together. No caller token is used
as the introspection credential. Token decisions cache for up to 30 seconds
(negative decisions three seconds), at most 1024 entries, bounded by expiry.
OAuth endpoints require HTTPS except loopback development servers; requests have
three-second timeouts and do not follow redirects.

**Magicentry today:** set `KAGI_CRAWLER_OAUTH_OPAQUE_USERINFO=1` to validate opaque
access tokens at the discovered userinfo endpoint when no introspection endpoint
is advertised. This is explicitly **weaker than MCP audience-bound authorization**:
userinfo cannot prove the token was issued for this resource. It returns identity,
not granted scopes, so leave required scopes unset for magicentry today. JWT or
introspection validation failures never downgrade to userinfo. Disable this opt-in
once the AS issues audience-bound JWTs or supports introspection.

In pi's user-level `~/.pi/agent/mcp.json` (pi still runs through your normal sandbox):

```json
{
  "mcpServers": {
    "kagi": {
      "url": "https://crawler.example.com/mcp",
      "oauth": { "clientId": "pi-kagi", "callbackPort": 8765, "scope": "openid profile email" }
    }
  }
}
```

Register that static client in magicentry with the exact redirect URI
`http://127.0.0.1:8765/callback`, then run `pi mcp login kagi` (or `/mcp login kagi`).
Magicentry currently has no DCR; configure the registered client rather than
expecting automatic registration. **Current magicentry discovery needs an AS fix
before pi login:** `scopes_supported` is emitted as a space-separated string,
but OIDC Discovery (and pi) requires a JSON array. It also omits
`code_challenge_methods_supported: ["S256"]` despite supporting S256 exchange;
strict MCP clients require that advertisement. The resource server does not
rewrite the AS's metadata; the example above assumes these AS fixes.
If the registration requires a secret, add
`"clientSecret": "${MAGICENTRY_PI_CLIENT_SECRET}"` inside `oauth`; never put its
literal value in configuration. Other compliant ASs can use pi's normal discovery
and supported registration mechanism without this static client configuration.

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
Examples (search, search, fetch, fetch, expand, crawl arguments respectively,
not HTTP JSON-RPC envelopes):

```json
{"query":"retry_limit version 2","num_results":10}
{"query":"Python reference","content_mode":"discovery"}
{"urls":["https://docs.python.org/3/library/asyncio.html"],"query":"cancellation"}
{"urls":["https://docs.python.org/3/library/asyncio.html"],"freshness":"fresh_only","content_mode":"full_text"}
{"evidence_ids":["<returned-version-bound-evidence-id>"]}
{"url":"https://docs.python.org/3/library/asyncio.html","path_prefix":"/3/library","max_pages":5}
```

Search defaults to acquired highlights, not provider snippets. Result counts are
limits, not guarantees. Search accepts query, num_results, content_mode and freshness;
fetch accepts urls, optional query/content_mode and freshness. Expand accepts only
evidence_ids; crawl accepts url, path_prefix and optional max_pages, deriving the
exact origin from url. Kagi filters,
per-call response budgets, cache ages, refresh/cache_only and traversal tuning
are not public controls; unsupported inputs are rejected. Provider dates mean
**update dates**, not verified publication dates. Postfilters, per-source errors, budget omissions and partial
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

### Persistent semantic memory

When enabled, successful public acquisitions enqueue background indexing without
waiting for embeddings. Search merges hybrid FTS5 BM25 + dense RRF(k=60) matches
from retained history **after live results**, excluding live URLs. These extra
results have `origin: "memory"`, `fetched_at`, verbatim highlights and expandable
evidence IDs, including in discovery/full-text/summary modes. `num_results` limits
live discovery and separately bounds supplementary memory passages. `fresh_only`
excludes memory. Live results have first claim on the shared response budget;
retained passages are omitted before altering live content. Memory lookup is
best-effort (five-second limit); discovery failures still return their normal error.
Dense retrieval never replaces lexical highlight selection or generates summaries.
Top-level `selection` query-term coverage describes live lexical highlighting,
not supplementary memory ranking.

**Privacy policy:** this private service retains acquired third-party text and
historical versions independently of the short-lived document cache. The SQLite
file lives at `$KAGI_CRAWLER_CACHE_DIRECTORY/memory.sqlite`; keep the volume private
and use one service process per volume. Default retention is 1 GiB, oldest fetched
versions first, with no age expiry. Canonical versions are stored once alongside
passage text/vectors so `expand` works after cache eviction; evidence becomes
unavailable only when neither store retains the exact version. Reads do not renew
retention. The cap includes SQLite/FTS pages (compacted after eviction), excluding
transient transaction journals/filesystem overhead. It is a bounded history, not
a permanent archive.

The pinned community export is
`onnx-community/embeddinggemma-2-ONNX@daa72c51243991dfcaf9f9137d2c573d8f7790c0`;
Docker anonymously downloads the FP32 graph/data/tokenizer at build time and
verifies their SHA256 checksums. Local setup (no credentials):

```sh
uv sync --locked
uv run --locked python scripts/install_embedding_model.py .runtime/embeddinggemma2
export KAGI_CRAWLER_MEMORY_MODEL_DIRECTORY="$PWD/.runtime/embeddinggemma2"
```

Missing files or `KAGI_CRAWLER_MEMORY=0` disable memory; `/health` discloses why.
Runtime never downloads models. `KAGI_CRAWLER_MEMORY_INT8=1` selects the pinned
INT8 pair (provision it with the same installer plus `--int8`); default FP32/768d
was faster and more accurate in the feasibility smoke. The graph already pools
and normalizes; lower dimensions truncate and renormalize. Prefixes are
`task: search result | query: ...` and `title: ... | text: ...`. Canonical passage
windows are split at 6000 UTF-8 bytes, titles bounded to 512 bytes; the worker also
checks the actual 8192-token ceiling and never silently truncates. Navigation-dense
passages at the highlighter's link-density penalty floor are skipped.

| Memory operator setting | Default | Bounds / meaning |
| --- | --- | --- |
| `KAGI_CRAWLER_MEMORY_CAP_BYTES` | 1073741824 | 1 MiB–10 GiB; independent of document cache |
| `KAGI_CRAWLER_MEMORY_RETENTION_SECONDS` | 0 | 0 disables age expiry; up to 10 years |
| `KAGI_CRAWLER_MEMORY_QUEUE_CAPACITY` | 8 | 1–64 documents; full queue skips ingestion and counts drops |
| `KAGI_CRAWLER_MEMORY_THREADS` | 4 | 1–16 inference threads; one passage per batch |
| `KAGI_CRAWLER_MEMORY_DIMENSIONS` | 768 | 128 / 256 / 512 / 768 |
| `KAGI_CRAWLER_MEMORY_WORKER_BYTES` | 6442450944 | 2–16 GiB address-space ceiling, not an RSS allowance |

Inference runs in a single owned subprocess, never the event loop; query and
background ingestion share it. Model/precision/dimension changes mark old vectors
inactive; old versions remain expandable, and reacquisition lazily reindexes them.
CPU cosine search scans vectors in bounded memory, O(passages × dimensions): use
only tens of thousands of passages initially; ANN is a follow-up, not provided by
the byte cap. Size deployment RAM/CPU for Chrome **and** the encoder; the earlier
3 GiB browser-only example is not a memory-enabled sizing recommendation.

Offline retrieval/throughput/latency/RSS/storage measurement (fresh store required):

```sh
uv run --locked python scripts/evaluation/run.py memory \
  --model-dir .runtime/embeddinggemma2 --store .runtime/memory-eval.sqlite \
  --output /tmp/kc-memory-metrics.json
```

The harness reports global passage Hit@1/5/10 against FTS5-only, separately for
original and ten identifier-free paraphrase cases. This is not budgeted highlighter
recall or proof that a passage answers a question.

### Generated summaries

Both search and fetch accept opt-in `content_mode: "summary"`: per-source Kagi
subscriber Summarizer output of our canonical Markdown, labelled `generated`,
not evidence. Search fixes length to `overview`; fetch uses `digest`; type is
always `summary`. With a query, summaries include the existing verbatim highlights
and evidence IDs; without one, fetch returns summaries only. Summary mode performs no cross-source
answer synthesis. Each source retains its content_version. Failures
add `summary_error` (`summary_unavailable` plus a fixed CLI/deadline reason) while
preserving available highlights and other sources; no paid API fallback occurs.

Summaries share discovery's CLI pool (2 running, 8 waiting, 10-second admission
wait), bounded stdout/stderr, isolated credential environment and pinned-version
check. Two workers run after acquisition, using only time remaining in the
request's 120-second deadline (including search discovery). Inputs are capped at
60 KiB UTF-8 to stay below per-argument OS limits: prefer the last section boundary,
then a paragraph boundary, otherwise a UTF-8-safe prefix. The CLI's `--filter`
reads separate items per line, not a multiline document. The summary discloses
`summary_input_truncated`; it may therefore describe only part of the version.
Summary records are cached by (content_version, length, type), share the document
cache's byte cap/retention/eviction and are not evidence or canonical text.

Generated summaries count toward the response budget **before highlight
admission**. Under pressure, omit atomic highlights before shortening summaries;
summary shortening sets `response_truncated`. Whole sources may still be omitted.
This opt-in can make one allowance-consuming upstream call per uncached source;
allowance accounting, rate limits, latency and real-session response compatibility
remain unverified. Flags and `data.markdown` are verified against the pinned CLI
source, not a live Kagi session.

### Research (generated answers)

`research({"query":"How do retry limits work?","tier":"small","verify_citations":false})`
uses Kagi Assistant with web access and the account's default model. `tier: "big"`
uses the single operator-configured `KAGI_CRAWLER_RESEARCH_MODEL` slug with
`--model ... --once --web-access`; without it, big returns `invalid_input` with
reason `research_model_not_configured`. Model selection is not a per-call control.
The JSON response labels `answer.text` as generated, includes generated
`references` (footnote ID, title, URL), tier, model when known, and `thread_deleted`.
These are untrusted generated content, **not evidence**. Research is not cached.

`verify_citations: true` fetches at most ten cited URLs through the normal
public-fetch policy/cache and query-focused highlight path. `verified_citations`
contains verbatim highlights with expandable evidence IDs or per-source errors.
This verifies source text, not the truth of the answer or its citation entailment.
The answer has response-budget priority, followed by references and evidence;
answer truncation, reference omissions and atomic evidence omissions are disclosed.

Research owns a separate CLI pool: one running, two waiting, ten-second queue
wait (`research_queue_full` / `research_queue_timeout`).
`KAGI_CRAWLER_RESEARCH_TIMEOUT_SECONDS` defaults to 300 (1–1800), including
admission, generation and optional citation fetching; cleanup has an additional
shielded ten-second allowance. Stdout/stderr share the existing 1 MiB invocation
cap. MCP progress notifications report generation/verification phases when the
caller supplies a progress token (MCP 1.26 lowlevel request context/session API).
The session token is confined to the isolated CLI environment, not argv or logs;
credential echoes in generated output are rejected.

Optional `KAGI_CRAWLER_RESEARCH_DAILY_SMALL` / `KAGI_CRAWLER_RESEARCH_DAILY_BIG`
nonnegative call caps default to 0 (unlimited). These count admitted attempts,
including failures, per UTC day **in this service process**, reset on restart,
and do not coordinate replicas or measure actual Kagi AI spend. A hit returns
`research_daily_cap`. No extra `kagi usage` calls are made.

**Unofficial subscriber automation:** Assistant may consume your account's AI
allowance, be restricted, or change without notice. Calls create account history.
After each call we attempt `assistant thread delete ID` whenever the CLI returned
a safe thread ID, including when answer validation fails. Cleanup failure never
fails a successful answer: inspect `thread_deleted: false`. The pinned CLI only
emits the ID on successful JSON output; errors, malformed output, cancellation
or timeout may leave unidentified history that we cannot safely delete. We never
enumerate/delete unrelated threads. The CLI's `--once` attempts temporary custom
assistant deletion after both prompt success and failure; cleanup errors fail the
CLI call, and killing it can interrupt that cleanup. Check account history/custom
assistants after failed calls. Flags, JSON (`message.markdown`,
`message.references_markdown`, `thread.id`) and cleanup are source-verified at
cb64cf51; live model availability, output compatibility, allowance/rate accounting
and successful server-side deletion still require a live Kagi session.

New HTML acquisitions conservatively remove semantic controls and decorative
permalinks/images, and recognize explicit API signatures and attributed article
headers as passage boundaries. Uncertain structure and technical qualifications
remain. Existing cached canonical text and historical evidence IDs are unchanged.

| Bound | Default | Hard limit / configuration |
|---|---:|---|
| Search result count | 10 | MCP `num_results`: 1–20 |
| Fetch URLs / expand evidence IDs | 1–10 | Separate tools, maximum 10 each |
| Serialized response bytes | 12,288 | 4,096–262,144; operator `KAGI_CRAWLER_RESPONSE_BUDGET_BYTES` only |
| Acquisition request deadline | 120 s | `KAGI_CRAWLER_DEADLINE_SECONDS`: 0.01–600 s, one deadline across composed pages |
| Discovery concurrency / waiting | 2 / 8 | Fixed shared discovery/summary CLI pool; excess is `discovery_queue_full` |
| Discovery admission wait / deadline | 10 s / 120 s | Fixed wait (`discovery_queue_timeout`); request deadline includes admission, version check and search (`discovery_timeout`) |
| CLI stdout + stderr | 1 MiB | Per invocation, at most two concurrent CLI jobs |
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
| Ordinary freshness | 1 hour | Fixed public cache age |
| Crawl | 5 pages / depth 1 / 15 s | MCP `max_pages`: 1–20; fixed frontier 32 and aggregate canonical bytes 8 MiB |

The 12 KiB compact-wire default leaves room for model clients such as pi that
pretty-print structured results under a 20 KB text cap. Near-limit frozen-corpus
responses measured 1.18–1.22× expansion (about 14.4 KB pretty-printed); this is
not a universal bound for arbitrary metadata. Raising the operator budget can
exceed client display limits. Discovery retains its independent 120 s deadline;
composed searches can spend up to 120 s in discovery plus 120 s in acquisition.
Use client call timeouts above 240 s at defaults. Per-page caps remain unchanged.

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

Tool responses are structured-only MCP `CallToolResult` objects:
`content=[]`, `structuredContent=<data>`, and `isError` (true for tool errors).
There is no text copy or pointer; clients must read `structuredContent`. Pi shows
its JSON to the model when `content` is empty, pretty-printed with 2-space indentation.

The byte budget counts compact serialized UTF-8 `structuredContent` (including
substantive metadata) plus the `content=[]` and `isError` result fields,
**not tokens**, HTTP headers or the JSON-RPC envelope. Pi's pretty-printed
model-visible text is larger than this wire representation. Highlights retain
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
refresh fails; `fresh_only` always acquires and **never** returns stale fallback.
Internal refresh/cache-only modes remain available to the cache/Retrieval layer,
not MCP clients. Crawl uses ordinary freshness.
Responses identify fetch time, content version and cache state; fallback includes
original age and refresh failure. Different queries reuse canonical documents,
not query-dependent selections. Evidence expansion refers to the **exact retained
version**, survives cache-mounted restarts, and never substitutes current text.
Oldest fetched versions expire/evict within cache bounds (reads do not renew them);
evidence absent from both cache and independent memory returns `evidence_unavailable`.
This is not a permanent archive.
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
  --endpoint https://your-private-edge/mcp --cases /path/to/public-cases.json
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
