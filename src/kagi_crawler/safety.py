"""Reject credential echoes recursively without modifying canonical quotations."""
from urllib.parse import unquote


def contains_secret(value, secrets):
    if isinstance(value, str):
        # URLs can reflect percent-encoded credentials, including repeated encoding.
        for _ in range(4):
            if any(secret and secret in value for secret in secrets):
                return True
            decoded = unquote(value)
            if decoded == value:
                break
            value = decoded
    elif isinstance(value, dict):
        return any(contains_secret(k, secrets) or contains_secret(v, secrets)
                   for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        return any(contains_secret(v, secrets) for v in value)
    return False


def safe_sources(data, secrets):
    # Do not redact text in place: that would break quote/version fidelity. Failed
    # outcomes intentionally omit contaminated URLs and nested errors as well.
    for key in ('sources', 'results'):
        for index, source in enumerate(data.get(key, [])):
            if contains_secret(source, secrets):
                data[key][index] = {'error': {'code': 'fetch_failed'}}
    if contains_secret(data, secrets):
        return {'error': {'code': 'fetch_failed'}}
    return data
