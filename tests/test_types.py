"""Shared error vocabulary is type-only: existing JSON strings stay unchanged."""
from typing import get_args, get_type_hints

import pytest

from kagi_crawler.budget import result
from kagi_crawler.discovery import DiscoveryError
from kagi_crawler.acquisition_policy import FetchError
from kagi_crawler.types import ErrorCode


@pytest.mark.parametrize('error_type', [FetchError, DiscoveryError])
def test_shared_error_codes_keep_exact_wire_strings(error_type):
    assert get_type_hints(error_type.__init__)['code'] == ErrorCode
    for code in get_args(ErrorCode):
        error = error_type(code)
        assert str(error) == code
        response = result({'error': {'code': error.code}}, error=True)
        assert response.content[0].text.encode() == b'{"error":{"code":"' + code.encode() + b'"}}'
        assert response.structuredContent == {'error': {'code': code}}


def test_fetch_details_keep_exact_wire_order():
    error = FetchError('blocked', reason='challenge', http_status=403)
    response = result({'error': {'code': error.code, **error.details}}, error=True)
    assert response.content[0].text == '{"error":{"code":"blocked","reason":"challenge","http_status":403}}'
