"""Generated summaries of bounded canonical Markdown, never evidence."""
import asyncio
import re

import anyio

from .cache import DocumentCache
from .discovery import Discovery, DiscoveryError
from .extraction import fragment_markdown
from .safety import contains_secret
from .types import ToolData

# Linux limits each argument to 32 pages (typically 128 KiB), independently of
# ARG_MAX. Leave ample headroom; --filter summarizes separate stdin lines, not
# one multiline document (pinned CLI main.rs run_summarize_filter).
MAX_INPUT_BYTES = 60 * 1024


def summary_input(text: str) -> tuple[str, bool]:
    encoded = text.encode('utf-8')
    if len(encoded) <= MAX_INPUT_BYTES:
        return text, False
    prefix = encoded[:MAX_INPUT_BYTES].decode('utf-8', errors='ignore')
    boundaries = list(re.finditer(r'\n(?=#{1,6} )', prefix))
    if boundaries:
        prefix = prefix[:boundaries[-1].start()]
    elif '\n\n' in prefix:
        prefix = prefix.rsplit('\n\n', 1)[0]
    return prefix, True


async def add_summaries(data: ToolData, discovery: Discovery, cache: DocumentCache,
                        secrets: tuple[str, ...], length: str, deadline: float) -> None:
    sources = iter(data.get('sources', data.get('results', [])))

    async def worker() -> None:
        for source in sources:
            if 'error' in source:
                continue
            version = source['content_version']
            cached = cache.summary(version, length)
            if cached is not None and not contains_secret(cached, secrets):
                # The Summarizer returns HTML; entries cached before conversion
                # are normalised on read so every summary is Markdown.
                source['summary'] = dict(cached, text=fragment_markdown(cached['text']))
                continue
            try:
                text, truncated = summary_input(source['content'])
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                with anyio.fail_after(remaining):
                    generated = await discovery.summarize(text, length)
                if contains_secret(generated, secrets):
                    raise DiscoveryError('discovery_parse_error')
                summary = {'text': fragment_markdown(generated), 'generated': True, 'provider': 'kagi',
                           'length': length, 'type': 'summary', 'content_version': version,
                           'summary_input_truncated': truncated}
                cache.store_summary(version, length, summary, source['fetched_at'])
                source['summary'] = summary
            except (DiscoveryError, TimeoutError) as error:
                reason = error.code if isinstance(error, DiscoveryError) else 'discovery_timeout'
                source['summary_error'] = {'code': 'summary_unavailable', 'reason': reason}

    # Fixed workers avoid flooding the shared CLI admission queue for 20 hits.
    # TaskGroup propagates caller cancellation and drains owned subprocesses.
    async with asyncio.TaskGroup() as group:
        for _ in range(discovery.processes.CONCURRENCY):
            group.create_task(worker())
