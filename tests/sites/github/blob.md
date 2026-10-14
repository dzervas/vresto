# Requests

[![Version](https://camo.githubusercontent.com/382f956798e0b325dd9be2d6138bad70efb516ca0e3d7ecd3217884841c9b2dd/68747470733a2f2f696d672e736869656c64732e696f2f707970692f762f72657175657374732e7376673f6d61784167653d3836343030)](https://pypi.org/project/requests/) [![Supported Versions](https://camo.githubusercontent.com/ba77a996ab2a619a9e5f23d6d89c2ebb3d1017640306e98784039efa8c138a9a/68747470733a2f2f696d672e736869656c64732e696f2f707970692f707976657273696f6e732f72657175657374732e737667)](https://pypi.org/project/requests) [![Downloads](https://camo.githubusercontent.com/cc96f075f680b13c4d6fa812e412a299c6d71b7f877b910073f924e9515d0287/68747470733a2f2f7374617469632e706570792e746563682f62616467652f72657175657374732f6d6f6e7468)](https://pepy.tech/project/requests) [![Contributors](https://camo.githubusercontent.com/17eb7024eadd72f38a6575be38debe9ed209e6434e0812f7a2e18b5799b288b7/68747470733a2f2f696d672e736869656c64732e696f2f6769746875622f636f6e7472696275746f72732f7073662f72657175657374732e737667)](https://github.com/psf/requests/graphs/contributors) [![Documentation](https://camo.githubusercontent.com/cd7edaf2bb18164c068b91b6ef00871efef884d07c0e5304e2e8969acdd59293/68747470733a2f2f72656164746865646f63732e6f72672f70726f6a656374732f72657175657374732f62616467652f3f76657273696f6e3d6c6174657374)](https://requests.readthedocs.io)

__Requests__ is a simple, yet elegant, HTTP library.

```python
>>> import requests
>>> r = requests.get('https://httpbin.org/basic-auth/user/pass', auth=('user', 'pass'))
>>> r.status_code
200
>>> r.headers['content-type']
'application/json; charset=utf8'
>>> r.encoding
'utf-8'
>>> r.text
'{"authenticated": true, ...'
>>> r.json()
{'authenticated': True, ...}
```

Requests allows you to send HTTP/1.1 requests extremely easily. There’s no need to manually add query strings to your URLs, or to form-encode your `PUT` & `POST` data — but nowadays, just use the `json` method!

Requests is one of the most downloaded Python packages today, pulling in around `300M downloads / week` — according to GitHub, Requests is currently [depended upon](https://github.com/psf/requests/network/dependents?package_id=UGFja2FnZS01NzA4OTExNg%3D%3D) by `4,000,000+` repositories.

## Installing Requests and Supported Versions

Requests is available on PyPI:

```
$ python -m pip install requests
```

Requests officially supports Python 3.10+.

## Supported Features & Best–Practices

Requests is ready for the demands of building robust and reliable HTTP–speaking applications, for the needs of today.

* Keep-Alive & Connection Pooling
* International Domains and URLs
* Sessions with Cookie Persistence
* Browser-style TLS/SSL Verification
* Basic & Digest Authentication
* Familiar `dict`–like Cookies
* Automatic Content Decompression and Decoding
* Multi-part File Uploads
* SOCKS Proxy Support
* Connection Timeouts
* Streaming Downloads
* Automatic honoring of `.netrc`
* Chunked HTTP Requests

## Cloning the repository

When cloning the Requests repository, you may need to add the `-c fetch.fsck.badTimezone=ignore` flag to avoid an error about a bad commit timestamp (see [this issue](https://github.com/psf/requests/issues/2690) for more background):

```shell
git clone -c fetch.fsck.badTimezone=ignore https://github.com/psf/requests.git
```

You can also apply this setting to your global Git config:

```shell
git config --global fetch.fsck.badTimezone ignore
```

---

[![Kenneth Reitz](https://raw.githubusercontent.com/psf/requests/main/ext/kr.png)](https://kennethreitz.org) [![Python Software Foundation](https://raw.githubusercontent.com/psf/requests/main/ext/psf.png)](https://www.python.org/psf)
