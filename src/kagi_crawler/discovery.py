"""Subscriber-only Microck discovery, isolated from service access credentials."""
from typing import TYPE_CHECKING
import json
import os
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


from .contract import SearchRequest
from .types import ErrorCode, ToolResult

if TYPE_CHECKING:
    from .server import Settings

class DiscoveryError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        self.code: ErrorCode = code
        super().__init__(code)



class SearchRow(BaseModel):
    model_config = ConfigDict(strict=True)
    url: str
    title: str = Field(min_length=1)
    rank: int | None = Field(default=None, ge=1)
    snippet: str = ""

    @field_validator("url")
    @classmethod
    def http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid discovery URL")
        return value


class SearchResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    data: list[SearchRow]

# Verified primary source: Microck/kagi-cli cb64cf514a99e527429468f92bc9f6b633a765e4
# (0.22.0), src/{cli,search,types,auth,main,local}.rs. Install that revision with:
# cargo install --locked --git https://github.com/Microck/kagi-cli \
#   --rev cb64cf514a99e527429468f92bc9f6b633a765e4 kagi
# Subscriber automation is unofficial, may break or be restricted by Kagi.
# Subscriber --limit truncates parsed results locally, not a provider count guarantee.


class Discovery:
    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        from .process import Processes

        self.processes = Processes(settings)

    async def search(self, request: SearchRequest) -> ToolResult:
        try:
            with anyio.fail_after(self.settings.timeout):
                async with self.processes.admission():
                    return await self._search(request)
        except TimeoutError:
            raise DiscoveryError('discovery_timeout') from None

    async def _search(self, request: SearchRequest) -> ToolResult:
        args = ["--error-format", "json", "search", "--format", "json",
                "--limit", str(request.num_results)]
        for name in ("region", "lens", "time", "from_date", "to_date", "order"):
            value = getattr(request, name)
            if value is not None:
                args.extend(["--" + name.replace("_", "-"), str(value)])
        if request.verbatim:
            args.append("--verbatim")
        if request.personalized is not None:
            args.append("--personalized" if request.personalized else "--no-personalized")
        args.extend(["--", request.query])
        stdout = await self._invoke(args)
        try:
            data = SearchResponse.model_validate(json.loads(stdout))
            results = [{"provider_rank": r.rank, "url": r.url, "title": r.title,
                        "provider_snippet": r.snippet} for r in data.data[:request.num_results]]
            if any(secret in value for r in results for value in (r["url"], r["title"], r["provider_snippet"])
                   for secret in (self.settings.session_token, self.settings.access_token)):
                raise ValueError("credential echoed by upstream")
        except (ValueError, ValidationError, UnicodeDecodeError):
            raise DiscoveryError("discovery_parse_error") from None
        # Microck HTML parser emits rank=null and can silently parse [] after markup
        # changes. Preserve array order without inventing ranks or verified absence.
        warnings = []
        if not results:
            warnings.append("empty_results_may_indicate_upstream_markup_change")
        if any(r["provider_rank"] is None for r in results):
            warnings.append("provider_rank_unavailable")
        return {"content_mode": "discovery", "postfilters": ["result_limit"],
                "discovery_limits": {"concurrency": self.processes.CONCURRENCY,
                    "queue_capacity": self.processes.QUEUE_CAPACITY,
                    "queue_wait_seconds": self.processes.QUEUE_WAIT,
                    "request_deadline_seconds": self.settings.timeout,
                    "invocation_output_bytes": self.settings.max_output_bytes},
                "date_semantics": "provider_update_date_not_verified_publication",
                "warnings": warnings, "results": results}

    async def _invoke(self, args: list[str]) -> bytes:
        with TemporaryDirectory(prefix="kagi-discovery-") as home:
            env = {"PATH": os.defpath, "HOME": home, "KAGI_CONFIG": home + "/config.toml",
                   "KAGI_SESSION_TOKEN": self.settings.session_token, "RUST_LOG": "off"}
            # Exact source revision is installed by scripts/install_kagi.py;
            # reject a different release rather than trusting changing flags.
            version_env = {k: v for k, v in env.items() if k != "KAGI_SESSION_TOKEN"}
            status, version, _ = await self.processes.run(["--version"], version_env)
            if status or version.strip() != b"kagi 0.22.0":
                raise DiscoveryError("discovery_cli_version")
            returncode, stdout, stderr = await self.processes.run(args, env)
        if returncode:
            try:
                error = json.loads(stderr)
                if not isinstance(error, dict):
                    error = {}
            except (ValueError, UnicodeDecodeError):
                error = {}
            code: ErrorCode = "discovery_failed"
            if error.get("http_status") == 429:
                code = "rate_limited"
            elif error.get("code") in ("authentication_error", "missing_credentials"):
                code = "upstream_authentication"
            elif error.get("code") == "parse_error":
                code = "discovery_parse_error"
            # Never forward message, suggestions, stderr or exception details.
            raise DiscoveryError(code)
        return stdout

    async def summarize(self, text: str, length: str) -> str:
        async with self.processes.admission():
            # Equals form keeps dash-leading Markdown literal to Clap.
            stdout = await self._invoke(["--error-format", "json", "summarize", "--subscriber",
                "--text=" + text, "--length", length, "--summary-type", "summary"])
        try:
            # Pinned CLI main.rs prints SubscriberSummarizeResponse as JSON;
            # data.markdown is generated Markdown, not the HTML output field.
            value = json.loads(stdout)["data"]["markdown"]
            if not isinstance(value, str) or not value.strip():
                raise ValueError("empty summary")
            return value
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            raise DiscoveryError("discovery_parse_error") from None
