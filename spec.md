# vresto: Private, Evidence-First Web Retrieval over HTTP MCP

## Problem Statement

Local and in-cluster agents need one shared web retrieval service that discovers sources through Kagi and returns useful page evidence without independent crawler installations or repeated search-then-fetch calls. Kagi CLI already exposes search over stdio MCP, but a transport bridge alone does not acquire documents, select evidence across pages, or manage browser lifetime and document reuse.

The operator wants a private, self-hosted Docker image using a Kagi subscriber session token, not a Kagi API token. The desired workflow resembles Exa's MCP retrieval tools, excluding proprietary datasets and autonomous research. Search snippets are often insufficient for verifying technical claims, while full pages waste agent context and short excerpts can omit crucial qualifications.

This is a personal infrastructure utility, not a public product. It must remain small, run on CPU without mandatory models or external databases, release browser resources when idle, and attempt stealth acquisition without promising universal access.

## Solution

Provide a single-image Streamable HTTP MCP service composing Kagi CLI discovery, Crawl4AI acquisition and evidence selection. Initial tools are search and fetch. Search returns up to ten results with query-focused verbatim highlights by default, following Exa's MCP workflow. Fetch obtains known URLs, fuller documents or surrounding context for previously returned evidence.

Use Patchright with a maintained Chromium version for best-effort automation stealth. One shared browser warms on any MCP request, serves serialized page acquisition and closes after one hour without browser work when no acquisition is active. A size-bounded document cache persists independently of browser lifetime.

Preserve attribution, relevant qualifications and explicit per-source failures. Ordinary requests may return labeled stale evidence after a failed refresh; explicitly fresh-only requests may not. Structured extraction, embeddings, rerankers and persistent corpus search are later, optional additions rather than initial dependencies.

## User Stories

1. As an operator, I want one Docker image, so that deploying the utility does not require a browser service or distributed stack.
2. As an operator, I want CPU-only baseline operation, so that a GPU or model server is not required.
3. As an operator, I want Kagi subscriber-session authentication, so that I do not need a Kagi API subscription or token.
4. As an operator, I want upstream credentials kept server-side, so that clients and arbitrary retrieved pages never receive them.
5. As an operator, I want the unsupported nature of subscriber automation documented, so that I understand its reliability and account-access risks.
6. As a Pi, OpenClaw or n8n user, I want private HTTP MCP access, so that my agents share one retrieval service.
7. As an operator, I want lightweight private access controls, so that this utility does not need public onboarding or multi-tenant infrastructure.
8. As an agent, I want ten search results with highlights by default, so that search immediately provides useful source evidence.
9. As an agent, I want discovery-only search when requested, so that simple lookups do not acquire every result page.
10. As an agent, I want supported Kagi search controls, so that discovery can be scoped appropriately.
11. As an agent, I want unsupported controls rejected and postfilters disclosed, so that approximate behavior is not presented as a provider guarantee.
12. As an agent, I want to fetch known URLs directly, so that authoritative sources do not require another search.
13. As an agent, I want clean Markdown preserving technical structures, so that headings, code, tables and warnings remain useful.
14. As an agent, I want JavaScript-rendered content, so that dynamic documentation is not silently empty.
15. As an agent, I want text-bearing PDFs, so that papers and reports can supply evidence.
16. As an agent, I want unsupported parsing reported explicitly, so that absent text is not mistaken for absent evidence.
17. As an agent, I want bounded related-page fetching, so that I can inspect documentation without launching an unrestricted crawl.
18. As an agent, I want highlights copied from retrieved documents, so that quoted evidence is verifiable rather than generated.
19. As an agent, I want nearby version constraints, warnings and negation retained, so that compact evidence does not misrepresent a claim.
20. As an agent, I want source URLs and section or PDF page references, so that I can cite and inspect evidence.
21. As an agent, I want one response budget across sources, so that retrieval does not overwhelm my context window.
22. As an agent, I want duplicate passages reduced without erasing qualifications, so that the budget is spent usefully.
23. As an agent, I want version-bound evidence IDs, so that I can request the surrounding context of exactly the text I received.
24. As an agent, I want expansion through the existing fetch tool, so that the tool surface remains small.
25. As an agent, I want evicted evidence versions reported as unavailable, so that a changed page is not silently substituted.
26. As an agent, I want full text when highlights are insufficient, so that I can investigate beyond selected passages.
27. As an agent, I want fetch timestamps, cache state and document versions, so that I understand evidence age and identity.
28. As an agent, I want explicit freshness policies, so that changing information can require a new fetch.
29. As an agent, I want stale fallback labeled with its original fetch time and refresh failure, so that resilience does not disguise freshness.
30. As an agent, I want fresh-only requests to fail instead of returning stale evidence, so that my stated freshness requirement is honored.
31. As an operator, I want documents cached across requests and container restarts, so that browser work is reused.
32. As an operator, I want a bounded cache, so that this utility does not grow into a permanent archive.
33. As an operator, I want browser warmup on any MCP request, so that an ensuing acquisition does not unnecessarily wait for browser startup.
34. As an operator, I want one shared browser rather than a browser per client, so that multiple agents do not multiply idle memory use.
35. As an operator, I want browser shutdown after one hour without browser work, so that an unused utility releases browser resources.
36. As an operator, I want serialized acquisition and bounded queuing, so that simultaneous clients cannot exhaust the host.
37. As an operator, I want browser recreation after idle shutdown or failure, so that existing MCP sessions can continue using the service.
38. As an agent, I want best-effort stealth acquisition, so that ordinary automation detection is less likely to block useful pages.
39. As an operator, I want maintained, keyless browser dependencies, so that stealth does not require a paid license or an obsolete browser.
40. As an agent, I want blocked/challenge outcomes distinguished, so that I can select another source instead of trusting challenge-page text.
41. As an agent, I want successful sources preserved when others fail, so that one unavailable page does not discard the request.
42. As an operator, I want cancellation, shutdown and bounded retries, so that abandoned work does not leave browsers or subprocesses running.
43. As an operator, I want private-network destination protection and untrusted-page handling, so that web acquisition cannot expose host or cluster services.
44. As an operator, I want useful diagnostics without credential or page-body leakage, so that failures can be investigated safely.
45. As a developer, I want public MCP integration tests with controlled upstream fixtures, so that behavior is reproducible without real credentials or unstable sites.
46. As an operator, I want optional enhancements evaluated independently later, so that extraction, semantic retrieval or storage are adopted for demonstrated value rather than architectural fashion.

## Implementation Decisions

### Scope and architecture

- Implement a small Python service using an established MCP SDK and native Streamable HTTP transport. A raw stdio-to-HTTP bridge is not sufficient for composed retrieval.
- Ship one Docker image containing the application, Kagi CLI, Crawl4AI, Patchright and its compatible Chromium runtime. Use a mounted cache directory; do not require Browserless, an external database, distributed workers or local inference.
- Keep public tool orchestration, discovery, acquisition/normalization, evidence selection and caching as focused responsibilities without speculative provider frameworks.
- Initial public tools are search and fetch. Do not advertise an unfinished extract tool. Local stdio support is not an initial requirement.
- Keep deployment private. Use simple operator-controlled service access separate from upstream credentials, with TLS at a trusted edge where needed. Do not implement public onboarding, OAuth client registration, billing or tenant management.

### Kagi discovery

- Use Microck's Kagi CLI as a bounded subprocess returning structured JSON. Pass arguments without shell interpolation and terminate subprocesses on cancellation or timeout.
- Subscriber-session access is the intended discovery path. A Kagi API token is neither required nor an automatic fallback.
- Treat the session token as a secret held only by the discovery integration. Never inject it into the crawler's browser profile, arbitrary URLs, tool responses or logs.
- Document that subscriber automation uses an unofficial HTML interface, may break when upstream markup changes, and may be restricted by Kagi. Report expired credentials and discovery/parser failures clearly; do not promise unattended availability beyond the upstream interface.
- Validate supported controls against the pinned CLI version and subscriber path. Region, lenses, time windows, explicit dates, ordering, verbatim and personalization may be exposed where supported.
- Do not equate Kagi update-date filtering with verified publication dates. Result count on the subscriber path is a limit, not a guarantee of finding that many results. Disclose post-retrieval filtering and reject unsupported controls.

### Tools and evidence contract

- Search defaults to up to ten results with acquired, query-focused highlights, matching Exa's MCP interaction pattern rather than its API, ranking or learned models. Fetch returned result pages subject to service bounds; do not silently apply an undisclosed five-page cap.
- Support explicit discovery-only search, highlights and full-text content modes. Fetch accepts a bounded URL list and optional relevance query, or an evidence expansion request. Validate mutually exclusive input forms before acquisition.
- Publish configurable defaults and hard limits for result/page counts, output size, crawl scope, bytes, queue capacity and duration. The agreed default result count is ten; response-budget and other numeric limits require implementation-time measurement rather than invented Exa parity.
- Source records distinguish requested and resolved URLs, title, provider rank where applicable, document type, fetch time, cache state, content version and returned content. Include publication time only when actually known.
- Keep provider snippets, verbatim highlights and full text distinguishable. Never substitute a snippet for failed acquisition without an explicit outcome.
- Use a shared response budget across evidence and substantial metadata. Document the tokenizer or label token estimates honestly. Indicate truncation, budget omissions and partial coverage; reject budgets too small for a meaningful valid response.
- Preserve successful sources alongside per-source failures. Whole-request errors cover invalid inputs, access rejection or discovery failure when discovery is necessary.
- Distinguish validation, upstream authentication, rate limiting, timeout, fetch failure, blocked/challenge page, unsupported format, policy rejection and unavailable evidence version. Do not present challenge pages as normal source evidence.
- Normalize documents with technical structures intact. Chunk by coherent sections and begin with lexical BM25 passage selection across acquired documents. Crawl4AI's page-level fit-Markdown filter alone is not the cross-source evidence-budget implementation.
- Preserve useful source diversity and remove exact/redundant evidence conservatively. Retain adjacent qualifications, version scopes, warnings and negation rather than optimizing only for short output.
- Highlights are copied from a canonical normalized document; no generative model is required for search or highlights.
- Evidence IDs identify a passage in a particular retained document version. Return URLs and section/PDF page references where available. Expansion through fetch resolves that same version and returns surrounding context within the requested budget.
- If a version is evicted, report it as unavailable. Never resolve an old evidence ID against newly fetched text. Keep versions only within cache retention/size limits; this is not a permanent historical archive.

### Acquisition and browser lifetime

- Reuse Crawl4AI as a library for browser-aware acquisition, normalization, basic PDF parsing and bounded traversal.
- Use Patchright's supported Crawl4AI integration with a maintained, compatible Chromium/driver pair. This is best-effort automation stealth, not an engine-wide fingerprint guarantee or a promise to bypass every site.
- Do not depend on a paid browser service or license key. Do not pin an obsolete CloakBrowser build merely to avoid current-build entitlement requirements. Camoufox is a later candidate only if representative-site testing justifies its additional integration cost.
- Own one shared browser at application level, not one browser per MCP client, socket or logical session. No elaborate per-client browsing-profile isolation is required for public-page retrieval.
- Any MCP request triggers or joins browser warmup when the browser is absent, including initialization and discovery-only requests. Coalesce concurrent startup attempts. Browser warmup must not unnecessarily block operations that do not need acquisition; disclose acquisition unavailability when needed.
- On initial warmup, start the idle interval. Thereafter track actual browser work. Discovery-only traffic may start an absent browser but does not indefinitely extend an existing browser's idle lifetime.
- Close the browser after one hour without browser work and only when no acquisition is active. Browser shutdown does not terminate MCP sessions or delete the cache. Later requests warm a new browser without requiring client reconnection.
- Default to one page acquisition at a time across clients. Bound the queue and its wait time; allow operator-configured concurrency after measurement. Discovery and cache-only work need not wait behind browser acquisition.
- Serialize acquisition within composed search/fetch requests while preserving partial results and explicit deadlines. Keep resource bounds on HTML/PDF downloads and traversal as well as Chromium navigation.
- Bound subpage traversal by pages, depth, time and allowed site scope; it is opt-in, not an exhaustive site-crawl promise.
- Support text-bearing PDFs. Report parse failures and unsupported scanned/layout-heavy documents rather than claiming robust OCR or scientific-document reconstruction.
- Handle browser failure and recreation; propagate cancellation and release crawler connections, pages, drivers and subprocesses during shutdown. Do not let an idle teardown race interrupt active acquisition.

### Cache and freshness

- Persist canonical document content and source/version metadata in one mounted, size-bounded cache directory. Browser lifetime and document retention are independent.
- Cache acquisition separately from query-dependent evidence selection. Different questions may reuse one document without reusing irrelevant highlights.
- Support ordinary cache use, maximum age, forced refresh/fresh-only and cache-only behavior. Report actual fetch times and cache state.
- When ordinary refresh fails, stale content may be returned with its original timestamp, an explicit stale flag and the refresh failure. Never perform that fallback for a fresh-only request.
- Use content fingerprints to identify document versions. Avoid URL normalization that removes meaningful parameters. Retain referenced versions only within the cache cap and evict associated evidence metadata coherently.
- Set retention and eviction defaults during implementation. No vector database, indefinite archive or persistent semantic corpus is needed initially.

### Safety and operational boundaries

- Treat upstream credentials and service access credentials as secrets even though public-page browsing holds no private authenticated profiles.
- Treat fetched content as untrusted data. The service does not execute page instructions, expose arbitrary JavaScript/browser configuration, or crawl authenticated private sites.
- Reject non-HTTP(S), local-file and forbidden private/loopback/link-local/metadata destinations. Cover redirects, DNS changes, browser subrequests and PDF acquisition; private deployment alone is not a destination-policy defense.
- Apply simple bounded retries, per-origin backoff, request and decompressed-byte limits, navigation limits and cancellation. Retain only operational diagnostics needed for latency, cache use, browser state and acquisition failures; omit secrets and full document bodies by default.
- Best-effort stealth does not include proxy rotation, CAPTCHA-solving services, paid unblocking, paywall bypass or guaranteed anti-bot evasion. Report blocked sources and continue where possible.
- Preserve upstream license notices and Crawl4AI's appended attribution requirement in packaging. Prefer maintained keyless dependencies over proprietary or stale browser artifacts.

## Testing Decisions

- The confirmed primary integration seam is a real MCP client against the running HTTP server. Assert externally observable retrieval behavior rather than private module structure or worker implementation.
- The repository currently contains the specification but no application or inherited test suite. Build the integration harness around controlled Kagi responses and fixture websites/documents; do not claim an existing crawler test suite or released bridge in this repository.
- Test discovery-only search, ten-result default handling, composed highlights, direct fetch, full text, technical Markdown fidelity, rendered pages, text PDFs and bounded subpages.
- Verify verbatim highlights resolve to the returned document version, references are usable, qualifying context survives, and the documented response budget is respected. Test explicit omissions and mixed success/failure responses.
- Test evidence expansion through fetch against the same cached version, including document refresh, restart and version eviction. An old ID must never return unrelated current text.
- Test cache reuse across different queries, persistence across restart, cache-only misses, forced refresh, maximum age, size-bounded eviction, labeled stale fallback and rejection of stale fallback for fresh-only requests.
- Exercise subscriber authentication/parser failures, rate limits, malformed input, unsupported controls, fetch timeouts, unsupported formats and challenge-page outcomes without requiring real credentials.
- Test startup warmup from initialization and other MCP requests, shared startup across clients, serialized acquisition, queue limits, browser crash recovery and reuse after idle teardown. Observe browser process startup/exit as an external resource outcome, not private ownership fields.
- Use controlled time to test the one-hour idle policy without waiting an hour. Verify discovery-only traffic does not indefinitely retain an already warm browser and active acquisition is not interrupted by idle shutdown.
- Verify shutdown/cancellation release browser and subprocess resources, documents survive browser exit, and existing MCP sessions can trigger browser recreation.
- Test forbidden destinations through direct URLs, redirects, browser subrequests and PDF fetching. Fixture-network exceptions must be explicit and test-only. Ensure discovery credentials never reach fixture pages, responses or normal logs.
- Keep deterministic tests independent of public websites, real sessions and paid calls. Opt-in smoke tests may use authorized Kagi credentials and representative public sites to assess actual acquisition, challenges and stealth behavior.
- Measure cold/warm latency, idle memory before startup and after teardown, active peak memory and multi-client queue behavior on deployment hardware. No fixed RAM footprint or evasion success rate has been established by this conversation.
- Separate acquisition failures from passage-selection quality. Evaluate technical documentation, code identifiers, versions, warnings, tables, PDFs, JS pages and absent answers under equal evidence budgets.
- Later model experiments compare BM25, small reranking, dense and hybrid selection independently. Evaluate supported extraction fields separately from schema validity. Optional models do not become defaults solely because their cards advertise long context or strong benchmarks.

## Out of Scope

- Public product infrastructure: client onboarding, multi-tenant isolation, OAuth registration, billing and public service guarantees.
- Kagi API-token requirements, automatic paid-API fallback, account administration and exposing subscriber credentials to clients or crawled pages.
- Exact Exa API compatibility, Exa index/ranking parity, web-wide nearest-neighbor retrieval and access to Exa's existing archive.
- Proprietary datasets, entity enrichment, Websets-like management and CRM integration.
- Autonomous research, iterative investigations, final-answer agents and long-running job orchestration.
- Initial arbitrary-schema extraction or generated summaries; those are a subsequent optional phase.
- Mandatory embeddings, rerankers, vector storage, local generative inference, GPU, external browser services or distributed queues.
- Initial persistent semantic corpus search and permanent historical archiving.
- Browser services requiring paid licenses or license keys; obsolete browser pinning to avoid those requirements.
- CAPTCHA solving, proxy-rotation infrastructure, paid unblocking, paywall bypass and guaranteed detection evasion.
- Private authenticated crawling, browsing profiles, arbitrary JavaScript/CDP control and unrestricted crawl jobs.
- Initial scanned-PDF OCR, advanced layout reconstruction, scheduled monitoring and webhooks.
- Homelab deployment changes or client configuration in this repository.

## Further Notes

### Delivery sequence

1. Build the private HTTP MCP contract, session-based Kagi discovery, shared warmable Patchright browser, serialized acquisition, source records, bounds and partial errors.
2. Complete the initial evidence workflow: ten-result highlighted search, full-text/direct fetch, qualification-preserving lexical selection, persistent bounded cache, freshness behavior, evidence expansion, text PDFs and bounded subpages.
3. Add optional structured extraction through an operator-configured model endpoint after retrieval quality is established. Validate JSON schema and supporting evidence independently; do not bundle generative weights into the baseline image.
4. Evaluate a small CPU reranker first, then embeddings/hybrid retrieval if lexical selection demonstrably misses relevant passages. Neither requires a persistent vector database for small per-request document sets.
5. Consider vector-backed corpus search only if retained-content search becomes a demonstrated separate need. Keep local corpus results distinct from live web discovery and version/eviction behavior coherent.

### Research conclusions and remaining measurements

- Exa's current MCP search requests ten results and highlights by default; the agreed contract follows that interaction, not undocumented output budgets or learned-model quality.
- Crawl4AI's SDK does not automatically inherit its Docker server's egress/security protections. Those must be addressed in the composed service without exposing the upstream administration surface.
- Patchright is the smallest verified integration candidate. Camoufox offers engine-level patches but requires more integration work; CloakBrowser's keyless older build is not an acceptable reason to freeze Chromium security updates. Browserless Community's basic stealth is not its commercial unblocking feature set.
- Model roles remain separate: embeddings retrieve semantic candidates, rerankers rescore candidates, vector stores persist/search representations, and LLMs generate bounded extraction or summaries. BM25 plus versioned evidence works without any of them.
- MiniLM's small cross-encoder is a plausible CPU experiment. Qwen's 0.6B embedding and reranking models are larger optional candidates. Gemma 4 E2B and Qwen3.5-4B have substantially larger complete weight artifacts than their names might imply; none has been benchmarked for this deployment.
- Active RAM, cold-start latency, output-budget defaults, cache cap, per-request limits and site-level acquisition quality remain implementation-time measurements. Idle browser teardown is an explicit requirement, not a substitute for bounding active browser work.
- Findings were checked against primary documentation/source, including Crawl4AI v0.9.4. Browser integration and detection behavior have not been exercised in a running implementation. Pin and test compatible maintained releases rather than treating development documentation or vendor claims as guarantees.

### References

- Kagi CLI authentication, structured search and automation disclaimer: https://github.com/Microck/kagi-cli
- Kagi session lifecycle: https://help.kagi.com/kagi/privacy/private-browser-sessions.html
- Crawl4AI library release: https://github.com/unclecode/crawl4ai/releases/tag/v0.9.4
- Crawl4AI browser integration: https://github.com/unclecode/crawl4ai/blob/v0.9.4/crawl4ai/browser_manager.py
- Crawl4AI license and attribution: https://github.com/unclecode/crawl4ai/blob/v0.9.4/LICENSE
- Crawl4AI fit Markdown: https://docs.crawl4ai.com/core/fit-markdown/
- Crawl4AI PDF parsing: https://docs.crawl4ai.com/advanced/pdf-parsing/
- Patchright: https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python
- Camoufox alternative: https://github.com/daijro/camoufox
- CloakBrowser binary terms: https://github.com/CloakHQ/cloakbrowser/blob/main/BINARY-LICENSE.md
- Browserless community/commercial distinction: https://github.com/browserless/browserless
- Exa MCP search implementation: https://github.com/exa-labs/exa-mcp-server/blob/main/src/tools/webSearch.ts
- Exa contents and highlights: https://exa.ai/docs/contents/quickstart and https://exa.ai/docs/search/highlights
- Exa authoritative API description: https://exa.ai/docs/exa-spec.yaml
- Small reranker candidate: https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2
- Optional embedding and reranking candidates: https://huggingface.co/Qwen/Qwen3-Embedding-0.6B and https://huggingface.co/Qwen/Qwen3-Reranker-0.6B
