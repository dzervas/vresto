# Httpx not production ready under load #3100

State: Unanswered

## Description 1 — sacOO7 — 2024-02-19T07:16:19Z

* One of our customers reported [httpx performance issues when hitting limit of ~100k requests/minute](https://github.com/ably/ably-python/issues/554).

[![image](https://private-user-images.githubusercontent.com/16723785/305842461-cc67f22d-e96a-41c5-8ae5-a71fe6492ebe.png)](https://private-user-images.githubusercontent.com/16723785/305842461-cc67f22d-e96a-41c5-8ae5-a71fe6492ebe.png)

* I was able to reproduce the same using [httpx-load-testing-repo](https://github.com/sacOO7/locust-httpx-testing).
* Please note that multiple users are using `httpx` sync client singleton instance to make requests against `https://rest.ably.io/time`. You can check <https://github.com/sacOO7/locust-httpx-testing/blob/47df57a9beee398a4f625dde736a39435de6e807/httpx_user.py#L41>

Following were my observations for increase in number of users =>

1. Average response time increases as number of users increases.
2. We can also see spike in response time taken by some of the requests. This requests can take upto 2 seconds to respond.
3. If we set `httpx.Limits(max_keepalive_connections=100, max_connections=100, keepalive_expiry=120)`, we can see fewer spikes, but it's still not as good as `python-requests`. Average response time is still greater than time taken by `python-requests`

Observations posted here

1. [10 users against `python-requests` and `httpx` (Http2=true)](https://github.com/ably/ably-python/issues/554#issuecomment-1943669827)
2. [100 users against `python-requests` and `httpx` (Http2=true)](https://github.com/ably/ably-python/issues/554#issuecomment-1945781022)
3. [50 users against `python-requests` and `httpx` (Http2=true) with max connection set to 100](https://github.com/ably/ably-python/issues/554#issuecomment-1951853223)

## Comment 2 — lovelydinosaur — 2024-02-19T11:41:35Z

Maintainer

Ah locust looks really useful here thanks.  
 I'd like to dig into this with you since it seems like a really useful tool, tho it needs a bit of unpicking...

First up _don't compare HTTP/2 requests vs. HTTP/1.1 requests_.

We _deliberately don't have HTTP/2 on by default_ in `httpx`, and it shouldn't be assumed to have better response characteristics.

So then...

I tried your `locust` tests, here's what I'm doing...

* I've removed the config from `HttpxTestUser` and `RequestsTestUser`, so that we're just using the client defaults.
* I'm running the test against `https://rest.ably.io/time`.
* For a 10s duration.
* With a concurrency of 100 sessions.

Let's start by figuring out if we're ~ on the same page here.

Here's a sample of the results I get there...

__httpx__

```
Type     Name                       50%    66%    75%    80%    90%    95%    98%    99%  99.9% 99.99%   100% # reqs
--------|---------------------|--------|------|------|------|------|------|------|------|------|------|------|------
GET      /time                      40     42     43     44     50     60     79     92    190    190    190   1287
--------|---------------------|--------|------|------|------|------|------|------|------|------|------|------|------
         Aggregated                 40     42     43     44     50     60     79     92    190    190    190   1287
```

__requests__

```
Type     Name                       50%    66%    75%    80%    90%    95%    98%    99%  99.9% 99.99%   100% # reqs
--------|---------------------|--------|------|------|------|------|------|------|------|------|------|------|------
GET      /time                      37     39     41     42     47     74    130    130    160    160    160   1287
--------|---------------------|--------|------|------|------|------|------|------|------|------|------|------|------
         Aggregated                 37     39     41     42     47     74    130    130    160    160    160   1287
```

* Are you able to run a similar test so that we can see if we're in the same ranges here?
* "not production ready under load" doesn't look correct from what I'm seeing here... it's response times are significantly faster than `requests`/`urllib3` in the 95%-99% ranges here, and slightly slower in other ranges. So... a mixed bag.

## Comment 3 — sacOO7 — 2024-02-19T11:58:24Z

Author

Hi [@tomchristie](https://github.com/tomchristie) really appreciate the response!  
 Currently, [locust-httpx-testing](https://github.com/sacOO7/locust-httpx-testing) repo, is configured to simulate the problem our customer is facing. You can assume a scenario where you are running a flask app, a new thread is created for every single incoming request and then we use `httpx` singleton HTTP api to communicate with ably ( single host, so using http2 makes sense )  
 Some points on your test-run

1. I didn't quite understand by statement `removed the config from HttpxTestUser and RequestsTestUser`. Please note that both of those users are configure to use singleton `session` instance across multiple users. You can check [SingletonHttpxClient](https://github.com/sacOO7/locust-httpx-testing/blob/47df57a9beee398a4f625dde736a39435de6e807/httpx_user.py#L26C7-L26C27) and [SingletonRequestsClient](https://github.com/sacOO7/locust-httpx-testing/blob/47df57a9beee398a4f625dde736a39435de6e807/request_user.py#L25). So, singleton `session` instance is responsible for managing requests from multiple threads/users and managing connection pool for the same.
2. According to the problem reported by our customer, this problem occurs after running `httpx` for quite a good amount of time. You should run this test for minimum of `5 minutes`.
3. Try running it with both `Http2=True` and `Http2=False`.
4. What were the limits configured using `httpx.Limits` for given test run of 100 users?

## Comment 4 — sacOO7 — 2024-02-19T12:00:31Z

Author

Problem we have is with the `response time` spikes for some of the requests. You can also see them when `Http2=False`. So, I was curious if there was internal issue with `httpx connection pooling` here?  
 Also, customer is currently using `python-request` client to handle same no. of requests. When they switched to `httpx`, they started facing these issues : (

## Comment 5 — lovelydinosaur — 2024-02-19T14:34:19Z

Maintainer

Let's try to keep things really simple...

```python
import concurrent.futures
import requests
import httpx
import time

NUM_REQUESTS = 100
NUM_THREADS = 10
URL = "https://www.example.com"

print("URL:          ", URL)
print("NUM_REQUESTS: ", NUM_REQUESTS)
print("NUM_THREADS:  ", NUM_THREADS)
print()


# requests
client = requests.Session()
durations = []
success, failure = 0, 0

def send():
    for _ in range(NUM_REQUESTS):
        start = time.monotonic()
        client.get(URL)
        end = time.monotonic()
        duration = end - start
        durations.append(duration)


all_start = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
    for _ in range(NUM_THREADS):
        executor.submit(send)
all_end = time.monotonic()

durations = sorted(durations)

print("requests")
print("--------")
print("Total requests: ", len(durations))
print("Average: %.3f" % (sum(durations) / len(durations)))
print("Median:  %.3f" % (durations[int(len(durations) * 0.5)]))
print("95th:    %.3f" % (durations[int(len(durations) * 0.95)]))
print("99th:    %.3f" % (durations[int(len(durations) * 0.99)]))
print()


# httpx
client = httpx.Client()
durations = []

def send():
    for _ in range(NUM_REQUESTS):
        start = time.monotonic()
        client.get(URL)
        end = time.monotonic()
        duration = end - start
        durations.append(duration)

all_start = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
    for _ in range(NUM_THREADS):
        executor.submit(send)
all_end = time.monotonic()

durations = sorted(durations)

print("httpx")
print("-----")
print("Total requests: ", len(durations))
print("Average: %.3f" % (sum(durations) / len(durations)))
print("Median:  %.3f" % (durations[int(len(durations) * 0.5)]))
print("95th:    %.3f" % (durations[int(len(durations) * 0.95)]))
print("99th:    %.3f" % (durations[int(len(durations) * 0.99)]))
print()
```

I'm seeing these kind of results...

```
URL:           https://www.example.com
NUM_REQUESTS:  100
NUM_THREADS:   10

requests
--------
Total requests:  1000
Average: 0.105
Median:  0.102
95th:    0.115
99th:    0.380

httpx
-----
Total requests:  1000
Average: 0.097
Median:  0.094
95th:    0.101
99th:    0.375
```

Trying out a few different combinations and the results I'm seeing seem ~comparable?...

## Comment 6 — sacOO7 — 2024-02-19T15:25:43Z

Author

[@tomchristie](https://github.com/tomchristie) Thanks for creating a separate script. I am going to try this out for an actual scenario. Please note that, we need to increase `NUM_THREADS` here for simulating actual users above 10. Meanwhile you should try out <https://github.com/sacOO7/locust-httpx-testing> and run `locust` command for the same.

## Comment 7 — sacOO7 — 2024-02-19T18:51:45Z

Author

Okay, where I run it for `NUM_REQUESTS=1000` and `NUM_THREADS=50` on ubuntu machine of github codespaces ->  
 Seems it hangs for `httpx` run, the thread indefinitely waits for other thread to get unblocked : (

```
URL:           https://rest.ably.io/time
NUM_REQUESTS:  1000
NUM_THREADS:   50

requests
--------
Total requests:  50000
Average: 0.071
Median:  0.068
95th:    0.089
99th:    0.145
total time taken 3561.960
total time taken concurrent 72.432






^CTraceback (most recent call last):
  File "/workspaces/locust-httpx-testing/cli-script.py", line 64, in <module>
    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
  File "/usr/local/python/3.10.13/lib/python3.10/concurrent/futures/_base.py", line 649, in __exit__
    self.shutdown(wait=True)
  File "/usr/local/python/3.10.13/lib/python3.10/concurrent/futures/thread.py", line 235, in shutdown
    t.join()
  File "/usr/local/python/3.10.13/lib/python3.10/threading.py", line 1096, in join
    self._wait_for_tstate_lock()
  File "/usr/local/python/3.10.13/lib/python3.10/threading.py", line 1116, in _wait_for_tstate_lock
    if lock.acquire(block, timeout):
KeyboardInterrupt
^CException ignored in: <module 'threading' from '/usr/local/python/3.10.13/lib/python3.10/threading.py'>
Traceback (most recent call last):
  File "/usr/local/python/3.10.13/lib/python3.10/threading.py", line 1537, in _shutdown
    atexit_call()
  File "/usr/local/python/3.10.13/lib/python3.10/concurrent/futures/thread.py", line 31, in _python_exit
    t.join()
  File "/usr/local/python/3.10.13/lib/python3.10/threading.py", line 1096, in join
    self._wait_for_tstate_lock()
  File "/usr/local/python/3.10.13/lib/python3.10/threading.py", line 1116, in _wait_for_tstate_lock
    if lock.acquire(block, timeout):
KeyboardInterrupt:
```

## Comment 8 — sacOO7 — 2024-02-19T18:53:59Z

Author

On my local windows machine, `httpx` never hits total of `50000` requests, seems some of those requests somehow indefinitely waits for response/ blocks the running thread : (

```
URL:           https://rest.ably.io/time
NUM_REQUESTS:  1000
NUM_THREADS:   50

requests
--------
Total requests:  50000
Average: 0.097
95th:    0.235
total time taken 4854.233
total time taken concurrent 115.234

httpx
-----
Total requests:  30694
Average: 0.099
Median:  0.093
95th:    0.188
99th:    0.281
total time taken 3044.638
total time taken concurrent 91.156
```

## Comment 9 — sacOO7 — 2024-02-19T19:01:46Z

Author

For `NUM_REQUESTS=100` and `NUM_THREADS=30`, I consistently get total time taken to be ~38.075 seconds against ~6.618 taken by `python-requests`. [@tomchristie](https://github.com/tomchristie) I am using the same script provided. Can you please check if you are getting same results !  
 This is latest

```
URL:           https://rest.ably.io/time
NUM_REQUESTS:  100
NUM_THREADS:   30

requests
--------
Total requests:  3000
Average: 0.071
Median:  0.067
95th:    0.077
99th:    0.253
total time taken 213.310
total time taken concurrent 7.289

httpx
-----
Total requests:  3000
Average: 0.378
Median:  0.092
95th:    1.082
99th:    3.067
total time taken 1132.953
total time taken concurrent 38.048
```

## Comment 10 (reply to comment 9) — lovelydinosaur — 2024-02-20T13:13:06Z

Maintainer

No not able to replicate that at all. (Even if I dial down the `limits=httpx.Limit(max_connections=...)` setting it's all still working nicely)

Make sure you're updated to the latest version of `httpcore`.

## Comment 11 (reply to comment 9) — sacOO7 — 2024-02-20T13:22:44Z

Author

Okay, I will check again from my side. Also, make sure you run script multiple times for same values. Out of 5 run, it will not work for minimum 2. I am able to reproduce this on ubuntu ( github codespaces ) and local windows machine

## Comment 12 (reply to comment 9) — sacOO7 — 2024-02-20T13:24:18Z

Author

It will surely break for

```
NUM_REQUESTS:  1000
NUM_THREADS:   50
```

Total number of requests made by `httpx` should be 50000. It never makes those many requests : (

## Comment 13 (reply to comment 9) — sacOO7 — 2024-02-20T13:42:07Z

Author

Also, try changing endpoint to `https://rest.ably.io/time` if you are using other endpoint :

## Comment 14 (reply to comment 9) — lovelydinosaur — 2024-02-20T14:21:39Z

Maintainer

I can't replicate this, no.  
 I'm only seeing sensible response characteristics.

Only suggestions here...

* Make sure your customer is using the latest version of `httpcore` (It has some improvements in how the connection pool handling works)
* Stick with HTTP/1.1, unless you've good evidence to suggest that HTTP/2 is an improvement for your use-case.

## Comment 15 — sacOO7 — 2024-02-19T19:09:18Z

Author

Ohkay it seems, things starts falling when I set NUM_THREADS>=25 ..

```
URL:           https://rest.ably.io/time
NUM_REQUESTS:  100
NUM_THREADS:   25

requests
--------
Total requests:  2500
Average: 0.070
Median:  0.066
95th:    0.076
99th:    0.257
total time taken 174.070
total time taken concurrent 7.180

httpx
-----
Total requests:  2500
Average: 0.134
Median:  0.083
95th:    0.110
99th:    3.068
total time taken 336.206
total time taken concurrent 13.827
```

## Comment 16 — sacOO7 — 2024-02-19T19:10:31Z

Author

Link to the script -> <https://github.com/sacOO7/locust-httpx-testing/blob/main/cli-script.py>

## Comment 17 — sacOO7 — 2024-02-20T12:45:06Z

Author

[@tomchristie](https://github.com/tomchristie) do you have any updates on the above?

## Comment 18 — MarkusSintonen — 2024-06-04T17:27:37Z

This might be related: [#3215](https://github.com/encode/httpx/issues/3215)

## Comment 19 — sacOO7 — 2024-06-04T17:40:33Z

Author

[@MarkusSintonen](https://github.com/MarkusSintonen) yes, surely seems related. I think only requests gives best performance under concurrent users and doesn't cause spikes. [@tomchristie](https://github.com/tomchristie) maybe it's time you test the library against http, https ( with http2 enabled ) with proper sandbox setup in place.
