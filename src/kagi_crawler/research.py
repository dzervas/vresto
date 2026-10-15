"""Generated Assistant answers with best-effort history cleanup and optional evidence."""
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import re
import json

import anyio

from .budget import apply_budget, wire_bytes
from .contract import FetchInput, ResearchInput
from .discovery import Discovery, DiscoveryError
from .process import Processes
from .retrieval import Retrieval
from .safety import contains_secret
from .types import ToolData


class ResearchProcesses(Processes):
    CONCURRENCY = 1
    QUEUE_CAPACITY = 2
    QUEUE_WAIT = 10


class Research:
    def __init__(self, discovery: Discovery, retrieval: Retrieval) -> None:
        self.settings = discovery.settings
        # Reuse the pinned CLI/auth/error boundary, but never discovery admission.
        self.cli = Discovery(replace(self.settings, timeout=self.settings.research_timeout))
        self.cli.processes = ResearchProcesses(self.cli.settings)
        self.retrieval = retrieval
        self.calls: dict[tuple[str, str], int] = {}

    async def run(self, request: ResearchInput,
                  progress: Callable[[str], Awaitable[None]]) -> ToolData:
        if request.tier == 'big' and not self.settings.research_model:
            return {'error': {'code': 'invalid_input', 'reason': 'research_model_not_configured',
                    'message': 'Big research requires KAGI_CRAWLER_RESEARCH_MODEL.'}}
        data: ToolData = {'tier': request.tier, 'thread_deleted': False}
        if request.tier == 'big':
            data['model'] = self.settings.research_model
        thread_id = None
        try:
            with anyio.fail_after(self.settings.research_timeout):
                async with self.cli.processes.admission():
                    day = datetime.now(timezone.utc).date().isoformat()
                    self.calls = {k: v for k, v in self.calls.items() if k[0] == day}
                    key = (day, request.tier)
                    cap = (self.settings.research_daily_small if request.tier == 'small'
                           else self.settings.research_daily_big)
                    if cap and self.calls.get(key, 0) >= cap:
                        return self.budget({**data, 'error': {'code': 'research_daily_cap',
                                'message': 'Process-local daily research call cap reached.'}})
                    self.calls[key] = self.calls.get(key, 0) + 1
                    await progress('Research started')
                    args = ['--error-format', 'json', 'assistant', '--format', 'json', '--web-access']
                    if request.tier == 'big':
                        assert self.settings.research_model is not None
                        args += ['--model', self.settings.research_model, '--once']
                    try:
                        raw = json.loads(await self.cli._invoke(args + ['--', request.query]))
                        # Capture the safe deletion target before validating the answer.
                        thread = raw.get('thread') if isinstance(raw, dict) else None
                        candidate = thread.get('id') if isinstance(thread, dict) else None
                        if (isinstance(candidate, str) and
                                re.fullmatch(r'[A-Za-z0-9_-]{1,256}', candidate) and
                                not contains_secret(candidate, self.retrieval.secrets)):
                            thread_id = candidate
                        data.update(self.parse(raw))
                    except (ValueError, KeyError, TypeError, UnicodeDecodeError, RecursionError):
                        raise DiscoveryError('discovery_parse_error') from None
                    finally:
                        # The CLI exposes no thread ID on error/timeout. Never list
                        # or delete unrelated account threads to guess which is ours.
                        if thread_id is not None:
                            with anyio.CancelScope(shield=True):
                                try:
                                    with anyio.fail_after(10):
                                        deleted = json.loads(await self.cli._invoke(
                                            ['--error-format', 'json', 'assistant',
                                             'thread', 'delete', thread_id]))
                                    ids = deleted.get('deleted_thread_ids') if isinstance(deleted, dict) else None
                                    data['thread_deleted'] = isinstance(ids, list) and thread_id in ids
                                except (DiscoveryError, TimeoutError, ValueError, TypeError, RecursionError):
                                    pass
                if request.verify_citations:
                    await progress('Verifying citations')
                    references = data['references']
                    verified = []
                    # Public fetch admits at most ten URLs; preserve reference order
                    # and disclose the rest rather than silently expanding work.
                    if references:
                        fetched = await self.retrieval.fetch(FetchInput(
                            urls=[r['url'] for r in references[:10]],
                            query=request.query, content_mode='highlights'))
                        verified.extend(fetched.get('sources', []))
                        data['citation_fetch_budget'] = fetched.get('budget', {})
                        if 'error' in fetched:
                            data['verification_error'] = fetched['error']
                    data['verified_citations'] = verified
                    data['citation_verification_omitted'] = max(0, len(references) - 10)
        except (DiscoveryError, TimeoutError) as error:
            code = error.code if isinstance(error, DiscoveryError) else 'discovery_timeout'
            code = code.replace('discovery_', 'research_')
            if 'answer' in data:
                data['verification_error'] = {'code': code}
            else:
                data['error'] = {'code': code}
        return self.budget(data)

    def parse(self, raw: object) -> ToolData:
        if not isinstance(raw, dict) or contains_secret(raw, self.retrieval.secrets):
            raise ValueError('unsafe response')
        message = raw['message']
        if not isinstance(message, dict):
            raise ValueError('invalid message')
        answer = message['markdown']
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('missing answer')
        references_md = message.get('references_markdown') or ''
        if not isinstance(references_md, str):
            raise ValueError('invalid references')
        # Pinned api.rs current_assistant_references_markdown emits footnote
        # definitions, not a structured references array. Documents are attachments.
        references = []
        for line in references_md.splitlines():
            match = re.fullmatch(r'\[\^([^\]]+)\]: \[(.*)\]\((.*)\)', line)
            if match:
                index, title, url = match.groups()
                references.append({'id': index, 'title': title, 'url': url})
            elif line.strip():
                # Preserve unparseable citations explicitly, never invent URLs.
                references.append({'title': line, 'url': ''})
        output: ToolData = {'answer': {'text': answer, 'generated': True, 'provider': 'kagi'},
                            'references': references}
        profile = message.get('profile') or {}
        # A configured big model is not evidence of the small account default.
        if isinstance(profile, dict) and isinstance(profile.get('model_name'), str):
            output['model'] = profile['model_name']
        return output

    def budget(self, data: ToolData) -> ToolData:
        if contains_secret(data, self.retrieval.secrets):
            return {'error': {'code': 'research_parse_error'},
                    'thread_deleted': data.get('thread_deleted', False)}
        limit = self.settings.response_budget_bytes
        # Reserve envelope/coverage space; answer has priority over citations.
        base = deepcopy({k: v for k, v in data.items() if k not in ('references', 'verified_citations')})
        while base.get('answer', {}).get('text') and wire_bytes({**base, 'references': [], 'sources': []}) > limit - 1024:
            text = base['answer']['text']
            base['answer']['text'] = text[:len(text) // 2]
            base['answer']['response_truncated'] = True
        references = list(data.get('references', []))
        omitted = 0
        while references and wire_bytes({**base, 'references': references, 'sources': []}) > limit - 1024:
            references.pop()
            omitted += 1
        base['references'] = references
        base['omitted_reference_count'] = omitted
        # Existing evidence budgeting retains whole quotes/IDs and discloses
        # omitted sources. Reserve bytes for the longer public collection name.
        output = apply_budget({**base, 'sources': data.get('verified_citations', [])}, limit - 32)
        sources = output.pop('sources', [])
        if 'verified_citations' in data:
            output['verified_citations'] = sources
        return output
