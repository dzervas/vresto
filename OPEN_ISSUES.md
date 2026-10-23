# Open issues from real agent use

Status: **open**, recorded 2026-10-23. Search latency is investigated separately in
[SEARCH_LATENCY.md](SEARCH_LATENCY.md); no runtime fixes are included here.

## Evidence scope

The audit scanned 3,034 accessible Pi JSONL session files, including subagent
histories and Code Mode call records. After deduplicating copied records and
excluding this repository's development/evaluation sessions, it found 364 calls
(259 fetch, 84 search, 20 research, one expand). Source-error counts below are
**lower bounds**: not all recorded outputs preserve the complete tool response.
A successful MCP call can still contain failed individual sources.

Pi evidence paths are relative to `~/.pi/agent/sessions/`. Useful examples:

- **P1:** `--home-dzervas-Lab-dotfiles--/2026-10-17T03-09-14-936Z_01a147d5-c737-771a-81a7-e228e57151d1.jsonl`
- **P2:** `--home-dzervas-Lab-cadara--/2026-10-18T15-31-15-129Z_01a14fa3-7639-75bc-912d-473420e4e685.jsonl`
- **P3:** `--home-dzervas-Lab-homelab--/2026-10-19T23-11-46-278Z_01a1566f-7065-7028-8dc4-0e8aebef7536.jsonl`
- **P4:** `--home-dzervas-Lab-dotfiles--/2026-10-20T01-55-46-205Z_01a15705-959d-7705-89fc-554b5bbf1c4b.jsonl`
- **P5:** `--home-dzervas-Lab-cadara--/2026-10-17T04-27-39-675Z_01a1481d-911a-71aa-b034-9d5414377d2c.jsonl`

The Pi calls cover October 13–21, with the real-use examples above on October
17–20. `kube.log` covers October 22–23. These are not matching client/server
traces, and the running server revision/configuration is not established.

## 1. Align MCP client timeouts with service budgets — high priority

- [ ] **Open: client/server timeout mismatch.**

**Observed:** Pi repeatedly reports `MCP request timed out after 60000ms` for
search, fetch and research. P1 lines 119 and 130 show research timeouts; P3 lines
14 and 22 show search and fetch timeouts. `kube.log` independently records
completed searches lasting 87.5, 125.6 and 196.7 seconds (lines 161, 11 and 155).
Current defaults are 300 seconds for retrieval and 900 seconds for research
(`src/server.py`, `Settings`).

**Scope:** determine effective timeouts in Pi, OpenCode and any intervening
transport/proxy, and align them with the intended service response budget and
bounded cancellation/cleanup behavior. Longer client timeouts do not resolve the
underlying search latency; do not treat them as the complete latency fix.

**Acceptance:** a controlled request exceeding 60 seconds either returns within
the agreed client budget or returns an intentional service timeout, rather than
an unrelated client cutoff. Cancellation must still drain owned resources.
Validate both clients when OpenCode evidence is available.

## 2. Resolve unavailable big-research capability — high priority

- [ ] **Open: clients select `tier="big"` when no big model is configured.**

**Observed:** seven of 20 real-use research calls were rejected with
`invalid_input / research_model_not_configured`; P1 line 116 and P2 line 42 are
examples. Another nine hit client timeouts, two hit research queue timeouts and
one hit queue-full, leaving only one successful research call overall.

**Verified behavior:** `Research.run` explicitly rejects big research when
`VRESTO_RESEARCH_MODEL` is absent (`src/research.py:38–40`).

**Scope:** decide whether to configure the intended model or make unavailable
capability sufficiently explicit that clients do not repeatedly request it.
Do not silently substitute a different tier/model.

**Acceptance:** advertised/documented capability matches deployment settings;
small research remains usable and big research either runs with the configured
model or is clearly unavailable before repeated paid-work attempts.

## 3. Revalidate admission failures against the deployed version — medium priority

- [ ] **Open: concurrent work produced queue failures in historical usage.**

**Observed:** P2 line 45 contains two `research_queue_timeout` responses, one
`research_queue_full` response and one 60-second client timeout during a
four-call research batch. Five recoverable acquisition source failures were
`queue_timeout`, involving GitHub and Docker documentation; P1 line 29 and P5
line 20 are examples.

**Important qualification:** these records predate the current deadline
migration. Current `src/process.py` uses concurrency-limited admission sharing
the request deadline, without the old independent queue capacity/wait rejection;
`src/acquisition.py` similarly waits under the request deadline. The old queue
codes are not proof that the same defect remains in the deployed service.

**Scope:** establish image revision and effective settings, then repeat bounded
concurrent workloads at the CLI and browser admission boundaries. Preserve the
current shared-deadline semantics; do not restore obsolete queue knobs as a fix.

**Acceptance:** callers wait or time out according to the current documented
budget, peer cancellation does not leak capacity, and overload does not silently
turn ordinary concurrent research into immediately unusable calls.

## 4. Support bounded textual responses — high priority

- [ ] **Open: raw source files and JSON endpoints are rejected.**

**Observed:** at least 23 real-use source responses were `unsupported_format`,
including 12 from `raw.githubusercontent.com`, four from `api.github.com`, and
three from `registry.npmjs.org`. Other occurrences involved HTML-facing hosts
and cannot all be attributed to MIME handling from the available evidence.

**Verified behavior:** `Browser._render_document` accepts HTML/XHTML and PDF,
then rejects other non-PDF response bodies (`src/browser.py:265–279`). Empty
extracted Markdown also produces `unsupported_format` at serialization.

**Scope:** add an explicitly bounded text/JSON acquisition representation for
intended public textual formats, preserving URL screening, byte limits, secret
redaction, cache versions and evidence offsets. Keep unsupported binary formats
rejected. Distinguish empty extraction from unsupported MIME when diagnosing
HTML-facing failures; do not add host-specific exceptions to generic extraction.

**Acceptance:** local fixtures for plain text, raw source, Markdown and JSON
exercise the intended support; oversize/binary/credential-bearing inputs remain
safe; retained evidence expands the exact canonical version.

## 5. Reproduce recurring HTML fetch failures — medium priority

- [ ] **Open: useful documentation sites return opaque `fetch_failed` results.**

**Observed:** at least 23 recoverable real-use source failures were
`fetch_failed`. Repeated hosts include `opencode.ai` (five), `devenv.sh` (four),
`microvm-nix.github.io` (two), `docs.docker.com` (two) and `github.com` (two).
P4 lines 24 and 31 show repeated OpenCode documentation failures; P5 lines 18,
35, 40 and 46 show Docker/devenv failures.

**Unknown:** the records do not establish whether each failure is a bad URL,
HTTP error, native navigation failure, adapter failure or extraction failure.
`kube.log` is from a later period, so it cannot supply the missing boundary for
these calls.

**Scope:** replay representative public URLs with sanitized, fixed-boundary
failure diagnostics and capture a regression fixture/reproducer for each
confirmed class. Check HTTP status and redirects before blaming site adapters.
Change fragile markup handling only under `src/sites/` when actually applicable.

**Acceptance:** every selected reproducer either succeeds or returns an accurate
fixed failure classification, with tests for confirmed defects. Do not replace
real failures with success containing login/challenge/error-page text.

## 6. Diagnose document-limit failures without blindly raising limits — medium priority

- [ ] **Open: documentation sources hit `resource_limit`.**

**Observed:** six recoverable real-use failures affected `akos.ma`, `devenv.sh`,
`bun.sh` (two), `support.google.com` and `dev.opencascade.org`. P5 line 42 is one
example.

**Unknown:** existing result records do not identify which byte/resource boundary
was exceeded. Current limits cover rendered DOM and canonical text/downloads,
with additional PDF limits (`src/acquisition_policy.py`, `src/browser.py`,
`src/pdf.py`). A limit rejection is not by itself an implementation bug.

**Scope:** reproduce representative documents and identify the exact producing
boundary; determine whether excess boilerplate, missing expansion bounds or a
legitimate document size caused rejection. Preserve operator containment.

**Acceptance:** demonstrate the cause with a bounded fixture and either fix
unnecessary growth or document the intentional rejection. Any limit change
requires matching validation and boundary tests.

## 7. Make browser-profile retirement reliable — medium priority

- [ ] **Open: temporary-profile cleanup raises unhandled idle-task exceptions.**

**Observed:** three traces in `kube.log:62–83`, `95–116` and `128–149` end in
`OSError: [Errno 39] Directory not empty`, reached through
`Acquisition._idle -> _retire -> Browser.close -> Chromium.close`.
`src/chromium.py:158–160` calls `TemporaryDirectory.cleanup()` without handling
that failure.

**Qualification:** subsequent browser startups succeed. A remaining Chrome
writer/shutdown race is a hypothesis, not a reproduced cause; permanent service
failure has not been demonstrated.

**Scope:** reproduce retirement with owned processes/profile writers still
exiting; establish process termination and bounded cleanup ordering. Do not
merely suppress all cleanup errors or leave unbounded orphan processes/profiles.

**Acceptance:** repeated idle retirement, cancellation and restart cycles have no
unhandled task exceptions, leave no owned Chrome processes/profile directories,
and allow the next acquisition to recover.

## 8. Complete the OpenCode audit — evidence follow-up

- [ ] **Open: OpenCode session history was unavailable in the sandbox.**

Expose a session export/store without credentials, then inspect actual vresto
calls, outputs, client timeout behavior and retry/fallback patterns. Match
client/server timestamps and deployment revision where possible. This is an
evidence gap, not a confirmed OpenCode defect.

## Known limitation, not a newly diagnosed defect

One recovered Reddit source was correctly classified `blocked / login_required`.
Logged-out acquisition cannot guarantee access to such content. Existing beads
`kagi-crawler-9un` and `kagi-crawler-a2c` already track related classification and
authenticated-site decisions; this audit does not authorize changing acquisition
policy or implementing those features.
