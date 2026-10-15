"""Internal Retrieval/cache seam for controls deliberately absent from MCP."""
from contextlib import asynccontextmanager
import json

from kagi_crawler.budget import result
from kagi_crawler.contract import FetchRequest, SearchRequest, CrawlInput
from test_http import cli_response
from test_retrieval import retrieval


@asynccontextmanager
async def running(tmp_path, *, source=None, **settings):
    async with retrieval(tmp_path, source=source or cli_response(json.dumps({'data': [
            {'rank': i, 'url': f'https://example.org/{i}', 'title': f'Source {i}',
             'snippet': 'Provider summary'} for i in range(1, 13)]})), **settings) as service:
        yield service, None


class RetrievalSession:
    def __init__(self, service):
        self.service = service

    async def call_tool(self, name, arguments):
        arguments = dict(arguments)
        if name in ('fetch', 'search', 'expand'):
            arguments.setdefault('response_budget_bytes', self.service.response_budget_bytes)
        if name == 'fetch':
            data = await self.service.fetch(FetchRequest.model_validate(arguments))
        elif name == 'search':
            data = await self.service.search(SearchRequest.model_validate(arguments))
        elif name == 'expand':
            data = await self.service.fetch(FetchRequest.model_validate(arguments))
        else:
            data = await self.service.crawl(CrawlInput.model_validate(arguments))
        return result(data, error='error' in data)


@asynccontextmanager
async def client(service, **_):
    yield RetrievalSession(service)
