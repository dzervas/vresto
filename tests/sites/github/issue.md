# Certificate loading regression with HTTPAdapters in 2.32.3 #6730

State: Closed

## Description 1 — ricellis — 2024-05-31T10:26:46.000Z

It appears that in version `2.32.3` default certificates are no longer loaded for custom `HTTPAdapter` contexts when they were previously.

I guess this might be a duplicate/related to [#6726 (comment)](https://github.com/psf/requests/issues/6726#issuecomment-2138406456).  
 Also related to [#6710 (comment)](https://github.com/psf/requests/pull/6710#issuecomment-2137802782) - adding `load_default_certs()` resolves the issue, but this wasn't required in previous versions and thus makes upgrading to `2.32.3` breaking.

#### Expected Result

With the code below using `requests` version `2.32.2` I get the URL content with no error.

#### Actual Result

Using `2.32.3` I get:

```
requests.exceptions.SSLError: HTTPSConnectionPool(host='raw.githubusercontent.com', port=443): Max retries exceeded with url: /psf/requests/main/MANIFEST.in (Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1006)')))
```

#### Reproduction Steps

```python
import requests
import ssl
from requests.adapters import HTTPAdapter, DEFAULT_POOLBLOCK
from urllib3.util.ssl_ import create_urllib3_context

# adapted from https://github.com/IBM/python-sdk-core/blob/1c207385de627df5d12fd0a0ebd04717ce5bb29d/ibm_cloud_sdk_core/utils.py#L34
class SSLHTTPAdapter(HTTPAdapter):
    """Wraps the original HTTP adapter and adds additional SSL context."""

    def init_poolmanager(self, connections, maxsize, block=DEFAULT_POOLBLOCK, **pool_kwargs):
        """Create and use custom SSL configuration."""

        ssl_context = create_urllib3_context()
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        # ssl_context.load_default_certs() # Adding this resolves the certificate issue but it was not required before

        super().init_poolmanager(connections, maxsize, block, ssl_context=ssl_context, **pool_kwargs)

session = requests.Session()
http_adapter = SSLHTTPAdapter()
session.mount('https://', http_adapter)

print(session.get(url='https://raw.githubusercontent.com/psf/requests/main/MANIFEST.in').text)
```

#### System Information

```
$ python -m requests.help
```

```json
{
  "chardet": {
    "version": null
  },
  "charset_normalizer": {
    "version": "3.2.0"
  },
  "cryptography": {
    "version": ""
  },
  "idna": {
    "version": "3.4"
  },
  "implementation": {
    "name": "CPython",
    "version": "3.11.9"
  },
  "platform": {
    "release": "23.5.0",
    "system": "Darwin"
  },
  "pyOpenSSL": {
    "openssl_version": "",
    "version": null
  },
  "requests": {
    "version": "2.32.3"
  },
  "system_ssl": {
    "version": "30300000"
  },
  "urllib3": {
    "version": "2.2.1"
  },
  "using_charset_normalizer": true,
  "using_pyopenssl": false
}
```

## Comment 2 — jaraco — 2024-07-07T20:51:59.000Z

Contributor

This issue appears to be leading to widespread breakage. Have you considered yanking the release? It's personally cost me a good deal of time troubleshooting, distilling, and reporting the issue in [httpie/cli#1581](https://github.com/httpie/cli/issues/1581), to the point that users are suggesting to move away from requests (feels drastic, admittedly). Would the maintainers at least consider acknowledging the issue and giving some insight into the plan?

## Comment 3 — nateprewitt — 2024-07-07T21:09:00.000Z

Member

Hi [@jaraco](https://github.com/jaraco), we have a PR with the fix up already. We've been evaluating if there are any other breakages because this series of releases has been problematic.

Applying the patch or downgrading is the immediate fix. The reason it's not yanked is because this was part of a change for a CVE fix in 2.32.x.

## Comment 4 — Jamim — 2024-07-12T22:11:54.000Z

> we have a PR with the fix up already.

For those who are wondering, here it is:

* [Address certificate loading regression #6731](https://github.com/psf/requests/pull/6731)

## Comment 5 — thomassa — 2025-09-30T14:58:13.000Z

Is this issue fixed in release v2.32.5?

This issue has a comment from [@Jamin](https://github.com/Jamin) (thank you!) linking to a pull-request intended to fix the issue: [#6731](https://github.com/psf/requests/pull/6731)  
 but that was closed in favour of pull-request [#6767](https://github.com/psf/requests/pull/6767) which was merged on 2025-06-13.  
 Then there was a release v2.32.5 on 2025-08-18 with this in its release notes:

```
Bugfixes

The SSLContext caching feature originally introduced in 2.32.0 has created
a new class of issues in Requests that have had negative impact across a number
of use cases. The Requests team has decided to revert this feature as long term
maintenance of it is proving to be unsustainable in its current iteration.
```

## Comment 6 — sigmavirus24 — 2025-09-30T15:06:50.000Z

Contributor

Yes this should be addressed in 2.32.5
