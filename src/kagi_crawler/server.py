"""Private, SDK-native Streamable HTTP MCP; TLS belongs at the trusted edge."""
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hmac
import json
import os
import shutil

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from pydantic import ValidationError
from starlette.applications import Starlette
import uvicorn

from .contract import SearchInput
from .discovery import Discovery, DiscoveryError


@dataclass(frozen=True)
class Settings:
    access_token: str = field(repr=False)
    session_token: str = field(repr=False)
    command: tuple[str, ...] = (shutil.which("kagi") or "kagi",)
    # Conservative safety limits, not measured Exa-equivalent output budgets.
    # Both CLI version check and search share one deadline; each invocation's
    # stdout+stderr share one byte cap. Deployment latency/memory and live subscriber quality remain
    # unmeasured; deterministic fixtures require no session or paid calls.
    timeout: float = 30
    max_output_bytes: int = 1_048_576


def tool_result(data, *, error=False):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))],
                          structuredContent=data, isError=error)


def create_app(settings: Settings):
    if not settings.access_token or not settings.session_token:
        raise ValueError("Service access and upstream session credentials are required")
    discovery = Discovery(settings)
    mcp = Server("kagi-crawler")

    @mcp.list_tools()
    async def tools():
        return [Tool(name="search", inputSchema=SearchInput.model_json_schema(),
                     description="Discover sources using Kagi subscriber search (default limit 10; maximum 20). "
                     "Only discovery content is implemented. Provider snippets are not page evidence. "
                     "Count is a local limit, not a guarantee; dates filter provider update dates, "
                     "not verified publication dates. Subscriber automation is unofficial.",
                     annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                 openWorldHint=True, idempotentHint=False))]

    @mcp.call_tool(validate_input=False)
    async def call(name, arguments):
        # SDK JSON-schema validation echoes inputs in errors. Validate here instead
        # and never serialize ValidationError (which may contain client secrets).
        try:
            if name != "search":
                raise ValueError("unsupported tool")
            request = SearchInput.model_validate(arguments)
        except (ValidationError, ValueError):
            return tool_result({"error": {"code": "invalid_input", "message":
                "Invalid search input or unsupported control; see search input schema."}}, error=True)
        try:
            return tool_result(await discovery.search(request))
        except DiscoveryError as error:
            messages = {
                "upstream_authentication": "Kagi subscriber session missing, rejected or expired.",
                "rate_limited": "Kagi rate limited discovery; retry later.",
                "discovery_parse_error": "Invalid discovery output; upstream HTML/parser may have changed.",
                "discovery_failed": "Kagi discovery failed; no paid API fallback was attempted.",
                "discovery_timeout": "Kagi discovery exceeded the service deadline.",
                "discovery_output_limit": "Kagi CLI output exceeded the service byte limit.",
                "discovery_cli_version": "Install the pinned Microck Kagi CLI revision; release mismatch.",
            }
            return tool_result({"error": {"code": error.code, "message": messages[error.code]}}, error=True)

    manager = StreamableHTTPSessionManager(mcp, json_response=True)

    @asynccontextmanager
    async def lifespan(_):
        async with manager.run():
            try:
                yield
            finally:
                await discovery.processes.close()

    app = Starlette(lifespan=lifespan)

    async def private(scope, receive, send):
        if scope["type"] == "http":
            authorization = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(authorization, ("Bearer " + settings.access_token).encode()):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"access_rejected"}'})
                return
            if scope["path"] == "/mcp":
                await manager.handle_request(scope, receive, send)
                return
        await app(scope, receive, send)

    return private


def main():
    # uv sync --locked; provision both variables separately, then uv run kagi-crawler.
    # Never source a shell file, read CLI auth config, or use API fallback.
    settings = Settings(access_token=os.environ.get("KAGI_CRAWLER_ACCESS_TOKEN", ""),
                        session_token=os.environ.get("KAGI_SESSION_TOKEN", ""))
    # Bound graceful HTTP shutdown, including idle SDK SSE sessions; lifespan
    # then cancels MCP work and reaps remaining CLI process groups.
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8000, access_log=False,
                timeout_graceful_shutdown=2)
