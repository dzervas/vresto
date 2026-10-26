"""Subscriber-only Microck discovery, isolated from service access credentials."""
from typing import TYPE_CHECKING
import asyncio
import json
import os
import time
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


from .contract import SearchRequest
from .types import ErrorCode, ToolResult
from .safety import contains_secret
from .deadline import request_deadline, remaining
from .diagnostics import logger

if TYPE_CHECKING:
    from .server import Settings

class DiscoveryError(Exception):
    def __init__(self, code: ErrorCode, *, reason: str | None = None, phase: str | None = None,
                 exception_type: str | None = None) -> None:
        self.code: ErrorCode = code
        self.reason = reason
        self.phase = phase
        self.exception_type = exception_type
        self.diagnosed = False
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

# Verified primary source: Microck/kagi-cli fe20aa2cde1155e226dbaa313680193652efca7b
# (0.23.0), src/{cli,search,quick,types,auth,main,local}.rs. Install that revision with:
# cargo install --locked --git https://github.com/Microck/kagi-cli \
#   --rev fe20aa2cde1155e226dbaa313680193652efca7b kagi
# Subscriber automation is unofficial, may break or be restricted by Kagi.
# Subscriber --limit truncates parsed results locally, not a provider count guarantee.


class Discovery:
    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        from .process import Processes

        self.processes = Processes(settings)

    def _diagnostic(self, operation: str, phase: str, error: DiscoveryError, *, started: float,
                    reason: str | None = None, upstream_code: str = '-',
                    http_status: int | None = None, exit_code: int | None = None) -> None:
        if error.diagnosed:
            return
        error.diagnosed = True
        reason = reason or error.reason or {
            'discovery_timeout': 'service_deadline', 'discovery_output_limit': 'output_limit',
            'discovery_cli_version': 'version_mismatch', 'discovery_parse_error': 'invalid_or_unsafe_output',
        }.get(error.code, 'process_failure')
        try:
            logger.warning(
                'cli_failure operation=%s phase=%s code=%s upstream_code=%s reason=%s '
                'http_status=%s exit_code=%s process_phase=%s type=%s admitted=%d waiting=%d elapsed_ms=%.1f',
                operation, phase, error.code, upstream_code, reason,
                http_status if http_status is not None else '-', exit_code if exit_code is not None else '-',
                error.phase or '-', error.exception_type or '-', self.processes.admitted,
                len(self.processes.waiters), (time.monotonic() - started) * 1000)
        except Exception:
            # A diagnostic handler must not replace the original failure.
            pass

    async def search(self, request: SearchRequest) -> ToolResult:
        started, phase = time.monotonic(), 'admission'
        with request_deadline(self.settings.timeout):
            # Both commands share admission and the request deadline, but a failed
            # or timed-out Quick Answer must not discard completed discovery links.
            quick = asyncio.create_task(self._quick_answer(request)) if request.quick_answer else None
            try:
                with anyio.fail_after(remaining(self.settings.timeout)):
                    async with self.processes.admission():
                        phase = 'response'
                        data = await self._search(request)
                if quick is not None:
                    data.update(await quick)
                return data
            except DiscoveryError as error:
                self._diagnostic('search', phase, error, started=started)
                raise
            except TimeoutError:
                error = DiscoveryError('discovery_timeout')
                self._diagnostic('search', phase, error, started=started)
                raise error from None
            finally:
                if quick is not None:
                    quick.cancel()
                    with anyio.CancelScope(shield=True):
                        await asyncio.gather(quick, return_exceptions=True)

    async def _quick_answer(self, request: SearchRequest) -> ToolResult:
        started, phase = time.monotonic(), 'admission'
        try:
            with anyio.fail_after(remaining(self.settings.timeout)):
                async with self.processes.admission():
                    phase = 'response'
                    args = ['--error-format', 'json', 'quick', '--format', 'json']
                    if request.lens is not None:
                        args.extend(['--lens', str(request.lens)])
                    raw = json.loads(await self._invoke(args + ['--', request.query]))
            if not isinstance(raw, dict) or contains_secret(raw, (self.settings.session_token, self.settings.access_token)):
                raise ValueError('unsafe quick answer')
            text = raw['message']['markdown']
            references = raw['references']['items']
            if not isinstance(text, str) or not text.strip() or not isinstance(references, list):
                raise ValueError('invalid quick answer')
            items = []
            for reference in references:
                row = SearchRow.model_validate(reference)
                index = reference['index']
                if type(index) is not int or index < 0:
                    raise ValueError('invalid reference index')
                items.append({'index': index, 'title': row.title, 'url': row.url})
            return {'quick_answer': {'text': text, 'generated': True, 'provider': 'kagi',
                                     'references': items}}
        except DiscoveryError as error:
            self._diagnostic('quick', phase, error, started=started)
            return {'quick_answer_error': {'code': error.code}}
        except TimeoutError:
            error = DiscoveryError('discovery_timeout')
            self._diagnostic('quick', phase, error, started=started)
            return {'quick_answer_error': {'code': error.code}}
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, RecursionError):
            error = DiscoveryError('discovery_parse_error')
            self._diagnostic('quick', 'response', error, started=started)
            return {'quick_answer_error': {'code': error.code}}

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
            if any(secret and secret in value for r in results for value in (r["url"], r["title"], r["provider_snippet"])
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
                    "request_deadline_seconds": self.settings.timeout,
                    "invocation_output_bytes": self.settings.max_output_bytes},
                "date_semantics": "provider_update_date_not_verified_publication",
                "warnings": warnings, "results": results}

    async def _invoke(self, args: list[str]) -> bytes:
        # Operation labels come only from service-owned command positions, not
        # queries, Markdown, deletion targets or arbitrary executable arguments.
        operation = args[2] if len(args) > 2 and args[2] in ('search', 'quick', 'summarize', 'assistant') else 'other'
        if args[2:5] == ['assistant', 'thread', 'delete']:
            operation = 'assistant_cleanup'
        started, phase = time.monotonic(), 'version'
        exit_code, http_status = None, None
        upstream_code, reason = '-', None
        try:
            with TemporaryDirectory(prefix="kagi-discovery-") as home:
                env = {"PATH": os.defpath, "HOME": home, "KAGI_CONFIG": home + "/config.toml",
                       "KAGI_SESSION_TOKEN": self.settings.session_token, "RUST_LOG": "off"}
                version_env = {k: v for k, v in env.items() if k != "KAGI_SESSION_TOKEN"}
                exit_code, version, _ = await self.processes.run(["--version"], version_env)
                if exit_code or version.strip() != b"kagi 0.23.0":
                    raise DiscoveryError("discovery_cli_version")
                phase, exit_code = 'execute', None
                exit_code, stdout, stderr = await self.processes.run(args, env)
            if exit_code:
                malformed = False
                try:
                    envelope = json.loads(stderr)
                    if not isinstance(envelope, dict):
                        envelope, malformed = {}, True
                except (ValueError, UnicodeDecodeError, RecursionError):
                    envelope, malformed = {}, True
                value = envelope.get('code')
                upstream_code = value if isinstance(value, str) and value in (
                    'network_error', 'authentication_error', 'missing_credentials', 'parse_error',
                    'configuration_error', 'contract_error', 'batch_error') else 'unknown'
                status = envelope.get('http_status')
                http_status = status if type(status) is int and 100 <= status <= 599 else None
                reason = 'signal' if exit_code < 0 else 'invalid_error_output' if malformed else 'cli_error'
                if http_status is not None:
                    reason = 'http_status'
                elif upstream_code == 'network_error':
                    reason = 'upstream_network'
                    message = envelope.get('message')
                    # Pinned http.rs emits these fixed markers. Classify them,
                    # never retain/log its URL-bearing message or response body.
                    if isinstance(message, str):
                        if message.startswith('network error: request to ') and ' timed out before Kagi responded.' in message:
                            reason = 'upstream_timeout'
                        elif message.startswith('network error: could not connect to '):
                            reason = 'upstream_connection'
                code: ErrorCode = "discovery_failed"
                if envelope.get('http_status') == 429:
                    code = 'rate_limited'
                elif envelope.get('code') in ('authentication_error', 'missing_credentials'):
                    code = 'upstream_authentication'
                elif envelope.get('code') == 'parse_error':
                    code = 'discovery_parse_error'
                raise DiscoveryError(code)
            return stdout
        except DiscoveryError as error:
            self._diagnostic(operation, phase, error, started=started, reason=reason,
                             upstream_code=upstream_code, http_status=http_status, exit_code=exit_code)
            raise
        except asyncio.CancelledError:
            try:
                logger.info('cli_cancelled operation=%s phase=%s elapsed_ms=%.1f',
                            operation, phase, (time.monotonic() - started) * 1000)
            except Exception:
                pass
            raise

    async def summarize(self, text: str, length: str) -> str:
        started, phase = time.monotonic(), 'admission'
        try:
            with request_deadline(self.settings.timeout):
                async with self.processes.admission():
                    phase = 'response'
                    # Equals form keeps dash-leading Markdown literal to Clap.
                    stdout = await self._invoke(["--error-format", "json", "summarize", "--subscriber",
                        "--text=" + text, "--length", length, "--summary-type", "summary"])
            # Pinned main.rs emits generated data.markdown, not its HTML field.
            value = json.loads(stdout)["data"]["markdown"]
            if not isinstance(value, str) or not value.strip():
                raise ValueError("empty summary")
            return value
        except DiscoveryError as error:
            self._diagnostic('summarize', phase, error, started=started)
            raise
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            error = DiscoveryError('discovery_parse_error')
            self._diagnostic('summarize', 'response', error, started=started)
            raise error from None
