# vresto

> **Only paid Kagi subscriptions are currently supported.**

A web search MCP server for AI assistants. Search with Kagi, read public pages
through Chrome, and retrieve query-focused quotes with source links. Runs locally
or in Docker; no external browser service or GPU required.

## Tools

- **search** — Find sources and return relevant passages, or discovery results only.
- **fetch** — Read URLs as highlights, full Markdown, or generated summaries.
- **expand** — Expand quoted evidence from the exact retained document version.
- **crawl** — Follow links within a specified site's path, with a page limit.
- **research** — Generate an answer through Kagi Assistant, optionally fetching
  cited sources for supporting quotes. May consume your account's AI allowance.

Search also supports generated summaries. Generated text is not evidence.
Cached documents and optional local semantic memory reduce repeated fetching;
`freshness: "fresh_only"` skips retained memory and requires fresh acquisition.
Use MCP `tools/list` for exact inputs and limits.

## Run with Docker

Provision two different secrets in your shell environment or secret manager:
`KAGI_SESSION_TOKEN` for your Kagi subscription, and `VRESTO_ACCESS_TOKEN` for
clients connecting to this server. Do not put secret values in source or images.

```sh
docker run -d --name vresto --init --restart unless-stopped \
  --cap-drop ALL --security-opt no-new-privileges --shm-size 256m \
  -p 127.0.0.1:8000:8000 \
  --mount type=volume,src=vresto-cache,dst=/data/cache \
  --env VRESTO_ACCESS_TOKEN --env KAGI_SESSION_TOKEN \
  ghcr.io/dzervas/vresto
```

Use `TARGETARCH=arm64` for ARM64. The build context is allowlisted by
`.dockerignore`; no credentials are needed during the build. The image includes
Chrome, a private virtual display, and the local embedding model.
Keep the cache volume private: it retains fetched documents and version history.

The CLI and Python import are both `vresto` (`vresto --help`, `import vresto`).
Server settings use the `VRESTO_` prefix; `KAGI_SESSION_TOKEN` keeps its name.

## Connect your MCP client

Use **Streamable HTTP** at `http://127.0.0.1:8000/mcp` with bearer authentication.
A typical client configuration is shown below; adapt field names to your client
and supply the access token through its secret/environment mechanism:

```json
{
  "mcpServers": {
    "vresto": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <VRESTO_ACCESS_TOKEN>" }
    }
  }
}
```

The placeholder represents your server access token, **not** your Kagi session.
For remote clients, use HTTPS through a trusted private reverse proxy and preserve
MCP streaming. Do not expose the server publicly without access controls.

## Safety and limitations

This is an **unofficial subscriber integration**, not the Kagi API or an official
Kagi product. Upstream changes can break it; check Kagi's terms and your account's
allowances. Research thread deletion is best-effort; failed calls may leave history.

Acquisition is for **public, logged-out pages only**: no supplied browser cookies,
login, CAPTCHA solving, or paywall bypass. Pages are untrusted data, not instructions.
URL screening is **not SSRF protection**; enforce network egress restrictions outside
the browser to block private/internal destinations, including redirects and subrequests.
