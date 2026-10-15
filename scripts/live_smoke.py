"""OPT-IN authorized acquisition/selection check; never part of deterministic CI.

Operator supplies a JSON list of {category,url,query,expected:[literal strings]}.
Categories: technical, identifiers, qualifications, tables, javascript, pdf, absent.
Outputs only category, error code and boolean evidence checks, never page bodies,
URLs, queries or secrets. Expected strings are operator-curated relevance labels,
not an automated answer-quality judge. An absent case uses expected=[] and reports
selection output without treating lexical nonmatches as verified absent answers.

Optional --exa-observations accepts an operator-normalized JSON list using the
same categories/booleans from AUTHORIZED Exa acquisition at the same evidence
budget. It does not acquire Exa or establish ranking/API parity. Exa gateway and
live Kagi access were unavailable during implementation; comparisons remain open.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def run(args):
    cases = json.loads(args.cases.read_text())
    token = os.environ.get('KAGI_CRAWLER_ACCESS_TOKEN', '')
    if not token:
        raise SystemExit('Operator must provision service access independently; no credential is printed.')
    categories = {'technical', 'identifiers', 'qualifications', 'tables', 'javascript', 'pdf', 'absent'}
    assert all(case['category'] in categories for case in cases)
    async with httpx.AsyncClient(headers={'Authorization': f'Bearer {token}'},
                                 timeout=250, trust_env=False) as http:
        async with streamable_http_client(args.endpoint, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                observations = []
                for case in cases:
                    full = await session.call_tool('fetch', {'urls': [case['url']], 'freshness': 'fresh_only',
                        'content_mode': 'full_text'})
                    canonical = full.structuredContent
                    if full.isError:
                        observations.append({'category': case['category'], 'acquisition_error': canonical['error']['code']})
                        continue
                    source = canonical['sources'][0] if canonical['sources'] else {}
                    error = source.get('error', {}).get('code') if source else 'budget_omitted'
                    observation = {'category': case['category'], 'acquisition_error': error,
                                   'full_text_truncated': 'content' in source.get('truncated_fields', [])}
                    if not error:
                        highlights = await session.call_tool('fetch', {'urls': [case['url']],
                            'freshness': 'ordinary', 'query': case['query']})
                        selected = highlights.structuredContent
                        records = selected.get('sources', [])
                        observation['selection_error'] = selected.get('error', {}).get('code') or next(
                            (record['error']['code'] for record in records if 'error' in record), None)
                        quotes = ' '.join(h.get('text', '') for s in records for h in s.get('highlights', []))
                        observation['acquired_expected'] = [text in source.get('content', '') for text in case['expected']]
                        observation['selected_expected'] = [text in quotes for text in case['expected']]
                        observation['highlight_count'] = sum(len(s.get('highlights', [])) for s in records)
                        observation['partial_coverage'] = selected.get('budget', {}).get('partial_coverage', False)
                    observations.append(observation)
                output = {'budget_bytes': canonical.get('budget', {}).get('limit_bytes'), 'observations': observations,
                          'limits': 'Truncated full text cannot establish acquisition completeness; lexical hits do not verify answers.'}
                if args.exa_observations:
                    exa = json.loads(args.exa_observations.read_text())
                    # Accept only normalized metrics, never raw upstream responses.
                    allowed = {'category', 'acquisition_failed', 'acquired_expected', 'selected_expected'}
                    assert all(set(item) <= allowed and item['category'] in categories and
                        all(type(value) is bool for key in ('acquired_expected', 'selected_expected')
                            for value in item.get(key, [])) and
                        type(item.get('acquisition_failed', False)) is bool for item in exa)
                    output['operator_supplied_exa_observations'] = exa
                    output['comparison'] = 'Compare acquisition errors separately from acquired-vs-selected expected strings; review qualifications/citations manually.'
                print(json.dumps(output, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorized-live', action='store_true', required=True,
                        help='Explicitly authorize upstream public-site calls via your private service.')
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--exa-observations', type=Path)
    asyncio.run(run(parser.parse_args()))
