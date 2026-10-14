# Address certificate loading regression - #6731

State: Closed

## Description 1 — nateprewitt — 2024-05-31T19:05:10Z

Member

#### Overview

This PR is intended to address two distinct issues introduced with the default cert optimizations originally introduced in 2.32.0. While we continue to refine the settings considered when opting into our optimized context, we'll no longer use the new default if any custom cert values are supplied. This addresses the concurrency issues raised in [#6726](https://github.com/psf/requests/issues/6726).

The second piece of this will be ensuring that when opting out of the default SSLContext, we're still supplying to the default CA Cert bundle correctly. This addresses the problems noticed in [#6710 (comment)](https://github.com/psf/requests/pull/6710#issuecomment-2137802782) and [#6730](https://github.com/psf/requests/issues/6730).

#### Considerations

We're now duplicating a decent chunk of the [logic from cert_verify](https://github.com/psf/requests/blob/0e322af87745eff34caffe4df68456ebc20d9068/src/requests/adapters.py#L324-L357) inside [_urllib3_request_context](https://github.com/psf/requests/blob/0e322af87745eff34caffe4df68456ebc20d9068/src/requests/adapters.py#L115-L128) but without our validation exceptions. That's a potential vector for behavioral shifts in the future. We _could_ consolidate some of this behavior in one place but it's going to require constructing a dict and using `setattr` on our `conn` in `cert_verify` while setting `pool_kwargs` in `_urllib3_request_context`. I started writing that up but it feels clunky. This is probably going to be a tradeoff of risking drift like we have with Session settings and binding the two behaviors together too tightly.

#### Testing

I'd like to codify the issues we've encountered through the whole 2.32.x saga in tests to hopefully avoid this in the future. Doing it cleanly without relying on external endpoints is proving to be a bit more involved than I'd like. I think we can harness some of the infrastructure added in [#6662](https://github.com/psf/requests/pull/6662), but I haven't had a chance to really dig into that.

## Review comment 2 — sigmavirus24 — 2024-05-31T19:42:07Z

Contributor

File: src/requests/adapters.py

```diff
         pool_kwargs["ssl_context"] = _preloaded_ssl_context
+    elif verify is True:
+        # Set default ca cert location if none provided
+        cert_loc = extract_zipped_paths(DEFAULT_CA_BUNDLE_PATH)
```

So this is what fixes [#6730](https://github.com/psf/requests/issues/6730) right? Because the custome Context + passing this should make their life better, right?

## Review comment 3 — nateprewitt — 2024-05-31T19:52:40Z

Member; Author

File: src/requests/adapters.py

```diff
         pool_kwargs["ssl_context"] = _preloaded_ssl_context
+    elif verify is True:
+        # Set default ca cert location if none provided
+        cert_loc = extract_zipped_paths(DEFAULT_CA_BUNDLE_PATH)
```

Yeah, this should address [#6730](https://github.com/psf/requests/issues/6730). [#6667](https://github.com/psf/requests/pull/6667) assumed we would always be using the default context which lead to removing these [two lines](https://github.com/psf/requests/pull/6667/files#diff-a4598bf8444e0feca489e60704c809c0dbfc47b90c4364216c931b497b3312d7L293-L294). That was "ok" because we were always forcing a load on the default context at import time.

Starting in 2.32.3, we've opted out of the default in the failure cases reported between 2.32.0-2.32.2. These are cases we're taking a custom context from the user but no longer setting the `DEFAULT_CA_BUNDLE` on the connection returned from what was previously `get_connection`. Since we don't know if we have the default SSLContext in `cert_verify`, I ported this into `_urllib3_request_context` to ensure we're passing this as `pool_kwargs` when we've disabled the default.

I validated this against the issue reported in [#6726](https://github.com/psf/requests/issues/6726) which was showing this regression after the `cert` fix in [2769cb6](https://github.com/psf/requests/commit/2769cb607d4e696e2fe70802d4246ccc5abd64a8). The other reported issues like [#6730](https://github.com/psf/requests/issues/6730) are also passing cleanly now.

## Comment 4 — stratakis — 2024-09-04T12:09:17Z

The regression is there for some time and the PR is still in draft. Is there some way to move this forward?

## Comment 5 — ghost — 2024-09-16T15:35:03Z

Maintainers, is there any update here? The latest version of HTTPie is mostly non-functional due to this issue, and we will need to drop it and migrate to other tools if this cannot be addressed.

There does not seem to be any blocking feedback on this PR - can any clarity be provided about what is currently preventing a merge? Is additional contribution required from the community?

## Comment 6 — Pjrich1313 — 2024-10-31T12:25:06Z

Debug and run nobe and then merge into repository

## Comment 7 — frenzymadness — 2024-12-17T10:16:26Z

Contributor

Could we please move this forward somehow? How can the community help?

This makes our lives harder because we don't know whether to wait for this fix or to adapt to the new behavior caused by the security fix.

## Comment 8 — Pjrich1313 — 2025-01-06T11:37:13Z

Unblock I have the proof merge when proof is available

## Comment 9 — Pjrich1313 — 2025-01-24T23:00:54Z

Thank you

## Comment 10 — adbuerger — 2025-05-02T15:27:21Z

I found this pull request and one of the associated issues ([#6730](https://github.com/psf/requests/issues/6730)) via the information given in [IBM/ibm-cos-sdk-python-core#30](https://github.com/IBM/ibm-cos-sdk-python-core/issues/30). As this seems to be a topic for multiple people, I wanted to ask if it is planned to release this change/fix in some way in the near future. Thank you for your time and for your reply.

## Comment 11 — adbuerger — 2025-06-02T13:55:44Z

[@nateprewitt](https://github.com/nateprewitt) [@Pjrich1313](https://github.com/Pjrich1313) [@sigmavirus24](https://github.com/sigmavirus24) I hope that I am tagging the right people for this topic since it is unclear to me from the discussion above what aspects are left unresolved regarding this PR. I understand that [@sigmavirus24](https://github.com/sigmavirus24) left comments on the implementations of [@nateprewitt](https://github.com/nateprewitt) and [@Pjrich1313](https://github.com/Pjrich1313) already approved the changes. So are there any aspects that still prevent these changes from being merged? I am not familiar with the details of this topic so I might not understand the remaining issues. Thank you very much for your consideration and for your work on this project.

## Comment 12 — nateprewitt — 2025-06-13T16:43:28Z

Member; Author

Resolving in favor of [#6767](https://github.com/psf/requests/pull/6767).
