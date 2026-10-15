# Add an asyncio.TaskGroup.cancel method #108951

State: Closed

## Description 1 — m-u-xyz — 2023-09-05T17:06:53.000Z

### Feature or enhancement

##### Proposal:

Trio/anyio have a way to cancel all tasks within a taskgroup, via `taskgroup.cancel_scope.cancel()`.

Asyncio doesn't have cancel scopes. The way to currently do this is to cancel the task which the taskgroup is a child of, but there are problems with this approach:

* the task is a private member of the taskgroup, so the task is inaccessible through it
* what about nested taskgroups?
* also passing the task around is additional overhead and risks cancelling it when the taskgroup is no longer active

To fix this, I propose to add a `taskgroup.cancel`method which behaves like `task.cancel` _if_ the taskgroup is still active (i.e. not aborting).

If this change is likely to be accepted I'm OK with writing a PR.

##### Has this already been discussed elsewhere?

I have already discussed this feature proposal on Discourse

##### Links to previous discussion of this feature:

<https://discuss.python.org/t/how-to-cancel-all-tasks-created-within-same-taskgroup/30215>

##### Linked PRs

* [gh-108951: document how to terminate an `asyncio.TaskGroup` #123837](https://github.com/python/cpython/pull/123837)
* [[3.13] gh-108951: Document how to terminate an asyncio.TaskGroup (GH-123837) #123956](https://github.com/python/cpython/pull/123956)
* [[3.12] gh-108951: Document how to terminate an asyncio.TaskGroup (GH-123837) #123957](https://github.com/python/cpython/pull/123957)
* [gh-108951: add TaskGroup.cancel() #127214](https://github.com/python/cpython/pull/127214)
* [gh-108951: document addition of TaskGroup.cancel() #149031](https://github.com/python/cpython/pull/149031)

## Comment 2 — gvanrossum — 2023-09-05T19:13:04.000Z

Member

I am afraid that I don't understand the problem well enough. Could you explain the need for this and the desired behavior in edge cases better? A bullet like "what about nested taskgroups" makes me wonder -- what about them? I have no idea what structure your app has (and I haven't been following Trio or anyio so referencing that doesn't help me).

## Comment 3 — m-u-xyz — 2023-09-06T06:24:11.000Z

Consider this test program.

```python
import asyncio

async def t1():
    async with asyncio.TaskGroup() as tg:
        tg.create_task(asyncio.sleep(1))
        tg._parent_task.cancel() # asyncio.current-task().cancel()
    assert False, "not reached"

try:
    asyncio.run(t1())
    assert False, "not reached"
except asyncio.CancelledError:
    print("Cancelled")
```

The cancellation always bubbles out of the taskgroup.

Here's the anyio equivalent:

```python
import anyio

async def t2():
    async with anyio.create_task_group() as tg:
        tg.start_soon(anyio.sleep,1)
        tg.cancel_scope.cancel()
    print("after anyio cancel")

anyio.run(t2)
```

While asyncio doesn't have cancel scopes, that's an implementation detail. The point is that, when cancelled via its own `.cancel` method, the taskgroup swallows the exception (after cleaning up its subtasks of course) and simply leaves the scope.

As to code structure, let's assume I have a complex program with parts that want to be restarted separately if/when their configuration changes. With a cancel-able taskgroup, this is easy, each subpart can do

```python
async def subsys(config):
    while True:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(wait_changed(tg.cancel, config))
            ... # now do whatever the config tells us to do
```

where `wait_changed` runs the callback (cancelling the taskgroup) as soon as the configuration starts changing, but waits for the change to stabilize before it itself exits.

This is difficult to do without `TaskGroup.cancel`. I'd have to raise some exception from within `wait_changed` instead of using a callback, and catch that outside of the taskgroup. Then I need to add a separate way to do the stabilization part, which probably means I need to create an object for the `wait_changed` part instead of simply starting a subtask. So these two lines I need for config-change handling blow up to ten lines of boilerplate I need to sprinkle my program with, which is kindof suboptimal.

## Comment 4 — gvanrossum — 2023-09-06T17:45:11.000Z

Member

It feels, at least to me, that "cancel" elicits all the wrong connotations, in an asyncio context, because it sounds like it would cause a CancelledError exception to propagate out.

It looks like the functionality you are looking for is a way to stop all the tasks running in the task group but then make the task group's `async with` block exit "peacefully" (not raising).

If you come up with a name other than "cancel" (maybe "stop"? but up to you) and a PR that implements it, I will consider it. Try not to use Trio/anyio as guidance or model, since we should not assume asyncio users are familiar with those.

## Comment 5 — gvanrossum — 2023-09-06T17:47:05.000Z

Member

> Ugh.

Can you please refrain from this kind of negative language? It's not helping your case.

## Comment 6 — m-u-xyz — 2023-09-06T17:49:36.000Z

Sorry. Reworded.

## Comment 7 — m-u-xyz — 2023-09-06T17:50:42.000Z

> If you come up with a name other than "cancel" (maybe "stop"? but up to you) and a PR that implements it, I will consider it.

Will do.

## Comment 8 — sobolevn — 2024-09-05T14:35:53.000Z

Member

I had some time today to experiment with this. Here's a patch that seems to do the very basic thing:

```diff
diff --git Lib/asyncio/taskgroups.py Lib/asyncio/taskgroups.py
index f2ee9648c43..968a539f5dd 100644
--- Lib/asyncio/taskgroups.py
+++ Lib/asyncio/taskgroups.py
@@ -150,25 +150,34 @@ async def __aexit__(self, et, exc, tb):
             # cycles (bad for GC); let's not keep a reference to
             # a bunch of them.
             try:
-                me = BaseExceptionGroup('unhandled errors in a TaskGroup', self._errors)
-                raise me from None
+                raise BaseExceptionGroup(
+                    'unhandled errors in a TaskGroup',
+                    self._errors
+                ) from None
+            except* _TaskGroupStop:
+                pass
             finally:
                 self._errors = None
 
+        # Direct `.stop()` call in `TaskGroup` body, like:
+        #
+        # async with TaskGroup() as tg:
+        #     if some_condition:
+        #         tg.stop()  <- here
+        if issubclass(et, _TaskGroupStop):
+            return True
+
+    def stop(self):
+        """Stop all the tasks in the task group."""
+        self._check_state()
+        raise _TaskGroupStop()
+
     def create_task(self, coro, *, name=None, context=None):
         """Create a new task in this group and return it.
 
         Similar to `asyncio.create_task`.
         """
-        if not self._entered:
-            coro.close()
-            raise RuntimeError(f"TaskGroup {self!r} has not been entered")
-        if self._exiting and not self._tasks:
-            coro.close()
-            raise RuntimeError(f"TaskGroup {self!r} is finished")
-        if self._aborting:
-            coro.close()
-            raise RuntimeError(f"TaskGroup {self!r} is shutting down")
+        self._check_state(coro)
         if context is None:
             task = self._loop.create_task(coro, name=name)
         else:
@@ -184,6 +193,19 @@ def create_task(self, coro, *, name=None, context=None):
             task.add_done_callback(self._on_task_done)
         return task
 
+    def _check_state(self, coro=None):
+        error = None
+        if not self._entered:
+            error = RuntimeError(f"TaskGroup {self!r} has not been entered")
+        if self._exiting and not self._tasks:
+            error = RuntimeError(f"TaskGroup {self!r} is finished")
+        if self._aborting:
+            error = RuntimeError(f"TaskGroup {self!r} is shutting down")
+        if error is not None:
+            if coro is not None:
+                coro.close()
+            raise error
+
     # Since Python 3.8 Tasks propagate all exceptions correctly,
     # except for KeyboardInterrupt and SystemExit which are
     # still considered special.
@@ -250,3 +272,7 @@ def _on_task_done(self, task):
             self._abort()
             self._parent_cancel_requested = True
             self._parent_task.cancel()
+
+
+class _TaskGroupStop(Exception):
+    """Exception to stop all the tasks in a task group."""
```

It allows to stop the `TaskGroup` gracefully:

* from tasks
* from the `TaskGroup`'s body

I used this code to test the concept:

```python
import asyncio
from asyncio import TaskGroup

from random import random

async def job(i, group=None):
    while True:
        print(f"Task {i}")
        await asyncio.sleep(1)
        if random() < 0.4:
            print(f"Task {i} cancelling others")
            group.stop()
            return

async def main():
    async with TaskGroup() as group:
        group.create_task(job(1, group))
        group.create_task(job(2, group))
        # TODO: try this as well
        # group.stop()

asyncio.run(main())
```

It seems to work for the general case, but I am not confident enough in it to submit a PR. Maybe someone else can improve it :)

## Comment 9 — picnixz — 2024-09-05T15:22:49.000Z

Member

Playing the devil's advocate but should we care of a `try-except` that could trap this exception?

```python
async with TaskGroup() as group: 
    group.create_task(job(1, group))
	try:
		group.stop()
	except:
		pass
```

Alternatively, can we use an event + waiter task to cancel the tasks in the group? or would this solution too tricky to implement?

## Comment 10 — sobolevn — 2024-09-05T19:14:57.000Z

Member

I don't know :)  
 That's why I didn't commit to the implementation.

## Comment 11 — gvanrossum — 2024-09-05T19:32:57.000Z

Member

I forget if I suggested this before -- I presume you could do something like add a new task to the task group that self-cancels, and then do something like `except* CancelledError:`? Assuming that stopping a task group is relatively rare, that would be most efficient, since it costs nothing if you don't need to stop a group. It would only be a few lines to turn that into a helper function in an app that does need it.

## Comment 12 — sobolevn — 2024-09-06T06:49:54.000Z

Member

Following [@gvanrossum](https://github.com/gvanrossum)'s idea, this can be done on unchanged `TaskGroup`:

```python
import asyncio
from asyncio import TaskGroup
from contextlib import asynccontextmanager
from random import random

class Stop(Exception):
    """Stop the task group."""

@asynccontextmanager
async def stoppable_task_group():
    try:
        async with TaskGroup() as group:
            yield group
    except* Stop as ex:
        print(f'{ex} was stopped')

async def stop(group):
    async def factory():
        raise Stop(group)
    await group.create_task(factory())

async def job(i, group=None):
    while True:
        print(f"Task {i}")
        await asyncio.sleep(1)
        if random() < 0.4:
            print(f"Task {i} cancelling others")
            await stop(group)
            return

async def main():
    async with stoppable_task_group() as group:
        group.create_task(job(1, group))
        group.create_task(job(2, group))

asyncio.run(main())
```

I changed `CancelledError` to be a custom `Stop`, because `CancelledError` has a bunch of special handling already, which we don't seem to require with this specific case.

## Comment 13 — picnixz — 2024-09-06T08:57:38.000Z

Member

Personally,

* I would add this example to the docs.
* I wouldn't add it to the stdlib because it would create an additional exception class. I think this solution has its place in a util.py file rather than as a dedicated functionality.
* If you still want to add it to the stdlib, I think we can create a stop-task factory instead and not expose the exception itself (or maybe we should, but in this case a better name should be found [bikeshedding yay!]). Though I need to think of whether I can still play the devil's advocate in this case or not...

## Comment 14 — gvanrossum — 2024-09-08T18:21:40.000Z

Member

Could one of you elaborate why we shouldn't just use CancelledError?

## Comment 15 — sobolevn — 2024-09-09T06:40:00.000Z

Member

[@gvanrossum](https://github.com/gvanrossum) I tried the same exact example code but with `asyncio.CancelledError` in both `raise` and `except*` and it sometimes didn't work correctly for me. When I run this example there are cases like:

```
Task 1 cancelling others
Task 2
Task 2
Task 2 cancelling others
```

Notice: `Task 2` executes once after the `Task 1` is cancelled. After that it is also cancelling other tasks.

I can also get cases like:

```
Task 1
Task 2
Task 1
Task 2 cancelling others
Task 1
Task 1
Task 1
Task 1
Task 1
(continues to run)
```

I am not sure exactly why, but it does not happen with custom exceptions.

This might be a bug on its own.

## Comment 16 — gvanrossum — 2024-09-10T00:24:58.000Z

Member

Oh, it's because of this comment (part of a docstring) in taskgroups.py:

```python
    Any exceptions other than `asyncio.CancelledError` raised within
    a task will cancel all remaining tasks and wait for them to exit.
```

There are numerous special cases for CancelledError that I don't feel like understanding right now, but I'm sure some of them are related to this documented behavior. It's not a bug.

Befor we go ahead with documenting the pattern for stopping a task group we should probably reason through or try via experiments how things should work when there are several "nested" task groups. ("Nested" in quotes because there's no concept of nesting task groups, but we certainly can create a task group in a task associated with another task group, and then we can consider the latter group the "parent" of the former.

* What happens when the parent task group gets stopped this way? Presumably the task that created the inner task group gets cancelled while in the `with` statement that manages the inner task group, and that's going to cancel all the tasks belonging to the inner group. I think.
* And what happens if the inner task group gets stopped this way? Since it catches the Stop exception it won't bubble out an exception, _unless_ some of its tasks raise an exception upon being cancelled (or had already raised some exception but other tasks in the group were still processing a cancellation). The exception bubbling out will be an ExceptionGroup; I _think_ that will be wrapped by the outer task group in another ExceptionGroup (but I haven't tried to check this, so I may be wrong -- possibly the ExceptionGroups are combined).

## Comment 17 — sobolevn — 2024-09-10T07:39:51.000Z

Member

Trio / AnyIO have clear anwsers to these complex questions thanks to CancelScopes:

* <https://trio.readthedocs.io/en/latest/reference-core.html#child-tasks-and-cancellation>
* <https://anyio.readthedocs.io/en/stable/cancellation.html>

I feel like this abstraction allows to clearly see what and where we want to bubble up or to suppress.

Basic example of how we can try to think about different task groups:

```python
async def nested_group(cancel_scope):
    async with TaskGroup(cancel_scope=cancel_scope) as tg2:
         ...

async def main():
    async with TaskGroup() as tg:
        # cancelling of `tg2` will cancel `tg`, since they are in the same scope:
        tg.create_task(nested_group(tg.cancel_scope))  
        # cancelling of `tg2` here will not bubble up, since they are independent:
        tg.create_task(nested_group(None))
```

However, even with scopes there are lots of cases that can break the flow: <https://anyio.readthedocs.io/en/stable/cancellation.html#avoiding-cancel-scope-stack-corruption>

## Comment 18 — gvanrossum — 2024-09-10T17:55:20.000Z

Member

Sorry, I have never managed to understand cancel scopes in Trio. And the models are just so different that the Trio abstractions just can't help asyncio.

## Comment 19 — belm0 — 2024-11-10T19:13:51.000Z

Contributor

In practice, cancelling a TaskGroup is very common, and the official guidance involves a lot of boilerplate (custom exception, extra try/except block, explicit task just to cancel).

Individual codebases will land on any of the following as a consequence, and none are satisfying:

1. use the recommended boilerplate throughout their code
2. use some hacky solution, such as accessing TaskGroup internals
3. write a convenient `TaskGroup` wrapper, or select from N 3rd party offerings --> inconsistency for such a common operation, not great for someone moving between codebases

Would this perhaps be something worth revisiting as a PEP?

Aside: Google AI is apparently confused between asyncio and anyio, giving the wrong answer on cancelling a TaskGroup. (I'd say it suggests that asyncio should have a straightforward way to cancel.)

[![Screen Shot 2024-11-10 at 11 01 06 AM](https://private-user-images.githubusercontent.com/1708631/384719336-b616c559-eab7-4117-87fd-1f325c1384af.png)](https://private-user-images.githubusercontent.com/1708631/384719336-b616c559-eab7-4117-87fd-1f325c1384af.png)

## Comment 20 — gvanrossum — 2024-11-10T21:17:12.000Z

Member

Google AI is irrelevant here. But I am definitely open to reconsidering this—I was on the fence anyway.

Can you propose crisp semantics for tg.stop() in all cases?

## Comment 21 — belm0 — 2024-11-11T01:50:51.000Z

Contributor

First, I thought to test if the internal `TaskGroup._abort()` method does what we want. I wrote some test code that can exercise the following origins of the abort:

* no_stop
* parent_body_stop
* child_stop
* nested_body_stop
* nested_child_stop

See `stop_task_group()` for proposed semantics.

If we have the cases of concern represented, and using `_abort()` looks viable, next steps would be:

* convert these tests to unit tests
* add stop() to TaskGroup, implemented as shown
* docs

```python
import asyncio


def stop_task_group(tg: asyncio.TaskGroup):
    """Stop a task group

    cancel() will be called on any tasks in the group that aren't yet done.
    This will cause the task group context manager to exit (without an exception).

    Currently implemented with TaskGroup internals.  It's proposed to expose
    this in the API as `TaskGroup.stop()`.
    """
    if not tg._aborting:
        tg._abort()


async def job(name, t, parent_tg: asyncio.TaskGroup, stop: bool):
    print(f"Task {name} start")
    await asyncio.sleep(t)
    if stop:
        print(f"Task {name} stopping parent group")
        stop_task_group(parent_tg)
    print(f"  Task {name} done")


async def nested_group_job(name, t, parent_tg: asyncio.TaskGroup, stop: bool,
                           child_stop: bool):
    print(f"Task {name} start (with nested task group)")
    async with asyncio.TaskGroup() as tg:
        tg.create_task(job(name + '-1', t/3, tg, stop=child_stop))
        tg.create_task(job(name + '-2', t, tg, stop=False))
        if stop:
            await asyncio.sleep(t/2)
            print(f"Task {name} nested group body stopping group")
            stop_task_group(parent_tg)
    print(f"  Task {name} done")


async def test(name: str, *, parent_body_stop=False, child_stop=False,
               nested_body_stop=False, nested_child_stop=False):
    print(f'---- test {name} ----')
    async with asyncio.TaskGroup() as tg:
        tg.create_task(job('1', 0.8, tg, stop=child_stop))
        tg.create_task(job('2', 1.0, tg, stop=False))
        tg.create_task(nested_group_job('3', 1.1, tg, stop=nested_body_stop,
                                        child_stop=nested_child_stop))
        if parent_body_stop:
            await asyncio.sleep(.25)
            print('Parent body stopping group')
            stop_task_group(tg)


asyncio.run(test('no_stop'))
asyncio.run(test('parent_body_stop', parent_body_stop=True))
asyncio.run(test('child_stop', child_stop=True))
asyncio.run(test('nested_body_stop', nested_body_stop=True))
asyncio.run(test('nested_child_stop', nested_child_stop=True))
```

output

```
---- test no_stop ----
Task 1 start
Task 2 start
Task 3 start (with nested task group)
Task 3-1 start
Task 3-2 start
  Task 3-1 done
  Task 1 done
  Task 2 done
  Task 3-2 done
  Task 3 done
---- test parent_body_stop ----
Task 1 start
Task 2 start
Task 3 start (with nested task group)
Task 3-1 start
Task 3-2 start
Parent body stopping group
---- test child_stop ----
Task 1 start
Task 2 start
Task 3 start (with nested task group)
Task 3-1 start
Task 3-2 start
  Task 3-1 done
Task 1 stopping parent group
  Task 1 done
---- test nested_body_stop ----
Task 1 start
Task 2 start
Task 3 start (with nested task group)
Task 3-1 start
Task 3-2 start
  Task 3-1 done
Task 3 nested group body stopping group
---- test nested_child_stop ----
Task 1 start
Task 2 start
Task 3 start (with nested task group)
Task 3-1 start
Task 3-2 start
Task 3-1 stopping parent group
  Task 3-1 done
  Task 3 done
  Task 1 done
  Task 2 done
```

## Comment 22 — graingert — 2024-11-11T08:38:55.000Z

Contributor

One interesting behaviour that TaskGroup.stop() gets is that it can immediately cancel tasks rather than waiting for a call_soon (or two!) that raising an exception out of a child task must wait for. Making it possible to use a TaskGroup in the current asyncio happy eyeballs code and building a `race` function that (although of course, not in the face of cancellation suppression) is possible to have only one winner

## Comment 23 — MarkofChina — 2024-11-14T01:25:00.000Z

1, you can explicitely cancel any task running inside TaskGroup block. (v3.11.5)  
 2, use `TaskGroup._abort()`.  
 3, raise any Exception from inside of TaskGroup, like WebsocketDisconnect, or any other customized Exception.  
 __BUT...__  
 believe me, if you need some method like 'stop' or cancel on TaskGroup, which means you have stepped on a wrong way in designing, please consider other better practice to end TaskGroup.

## Comment 24 — belm0 — 2024-11-14T03:05:36.000Z

Contributor

> if you need some method like 'stop' or cancel on TaskGroup, which means you have stepped on a wrong way in designing

Please explain. From my observations over more than 5 years developing a non-trivial, production, async app: short-circuiting of task groups is a very common, useful, and normal operation.

## Comment 25 — m-u-xyz — 2024-11-14T07:00:05.000Z

Seems wrong that we can stop a taskgroup immediately when it's unintentional, i.e. by raising an exception inside it, but not by design, and/or not without overhead that seems backwards (starting a new task to end a taskgroup … ugh).

Also of note: Trio doesn't have a way to stop a taskgroup either, __but__ it abstracts that idea out into a distinct concept ("cancel scope") — and helpfully attaches a cancel scope to every taskgroup. Fine we could do it that way instead, or indeed use anyio – which goes to considerable trouble to make asyncio behave as if cancel scopes were a thing here. Unfortunately that trouble comes with a heavy chunk of overhead. Also, IME 99% of the time cancel scopes are used either for timeouts — or as part of, surprise, a taskgroup, so given that asyncio already knows another way to do the former, being able to directly cancel (all of) a taskgroup seems like the natural way forward.

[@MarkofChina](https://github.com/MarkofChina) I respectfully submit that race-to-the-finish code patterns are not "wrong". They have many applications, and mostly-bug-free support for them matches a "cancellable taskgroup" design quite naturally. Another example: when a connection dies and I need to stop the workers, I want to stop them __now__. I do _not_ want them to stumble onto that dead connection and cause an avalance of `ExceptionGroup`-nested errors which make seamless handling of the original error all but impossible.

## Comment 26 — belm0 — 2024-11-17T18:11:59.000Z

Contributor

Repeating the proposed semantics and implementation below. Happy to take the next step and make a PR, but can I first have a thumbs up from a cpython dev that would sponsor this?

```python
def stop_task_group(tg: asyncio.TaskGroup):
    """Stop a task group

    cancel() will be called on any tasks in the group that aren't yet done.
    This will cause the task group context manager to exit (without an exception).

    Currently implemented with TaskGroup internals.  It's proposed to expose
    this in the API as `TaskGroup.stop()`.
    """
    if not tg._aborting:
        tg._abort()
```

## Comment 27 — arthur-tacca — 2024-11-17T23:17:47.000Z

I don't think just calling `_abort()` would work because it doesn't cancel the task hosting the task group, so if the body of the task group is still waiting then it won't be interrupted. I think you'd need to do the following [edit for clarity: in addition to calling `_abort()`] or at least something roughly like it, but I can't currently check:

```python
if not self._parent_cancel_requested:
    self._parent_cancel_requested = True
    self._parent_task.cancel()
```

## Comment 28 — belm0 — 2024-11-18T05:08:14.000Z

Contributor

Thank you for raising that case-- yes, the task group body should be cancelled too.

It seems this will need some new logic in the `TaskGroup` implementation, as calling `_parent_task.cancel()` always causes CancelledError to be raised out of the task group.

--> Since the exception is coming from inside the task group context manager, `__aexit__` must return True to suppress the exception.

## Comment 29 — belm0 — 2024-11-18T05:59:08.000Z

Contributor

Is there a bug in `TaskGroup.__aexit__()`? This code intends to stop propagation of `CancelledError` out of the task group, but it's neglecting to return `True` to signal the suppression.

[cpython/Lib/asyncio/taskgroups.py](https://github.com/python/cpython/blob/500a4712bb42355eeb785ed5b9d71507384d18bc/Lib/asyncio/taskgroups.py#L144-L149)

Lines 144 to 149 in [500a471](https://github.com/python/cpython/commit/500a4712bb42355eeb785ed5b9d71507384d18bc)

```
 if self._parent_cancel_requested: 
     # If this flag is set we *must* call uncancel(). 
     if self._parent_task.uncancel() == 0: 
         # If there are no pending cancellations left, 
         # don't propagate CancelledError. 
         propagate_cancellation_error = None
```

## Comment 30 — m-u-xyz — 2024-11-18T06:04:51.000Z

When you explicitly cancel a task that happens to run a taskgroup, you _want_ the exception to progress …

## Comment 31 — belm0 — 2024-11-18T06:12:17.000Z

Contributor

> When you explicitly cancel a task that happens to run a taskgroup, you want the exception to progress …

Then the following comment is misleading, since `__aexit__` returning None is going to propagate the incoming exception.

```
# If there are no pending cancellations left,
# don't propagate CancelledError.
```

For the `stop()` implementation, I'll need to add another internal flag to TaskGroup, and when it's set have the `if self._parent_task.uncancel() == 0` case `return True`.

## Comment 32 — arthur-tacca — 2024-11-18T09:28:33.000Z

[@belm0](https://github.com/belm0) Ah, very well spotted! There is no bug in the current code because if there is an exception raised in the parent task (i.e., there is something for `__aexit__()` to re-raise), there are three possibilities:

* The task itself directly raised an exception (e.g., the user code raised `RuntimeError`) in which case [`raise BaseExceptionGroup(...)`](https://github.com/python/cpython/blob/2b2ad244c076588da401ffa68fc2cad346da6667/Lib/asyncio/taskgroups.py#L173) is run.
* The task was cancelled because one of the child tasks raised a user exception and therefore `_abort()` was called to cancel the parent task. In that case, that same line of code `raise BaseExceptionGroup(...)` is still going to be run with that other task's exception.
* The task was cancelled due to some outside cancellation, and neither of the above is true (`self._errors` is empty), in which case [`raise propagate_cancellation_error`](https://github.com/python/cpython/blob/2b2ad244c076588da401ffa68fc2cad346da6667/Lib/asyncio/taskgroups.py#L156) is run directly.

In other words, if we ever get to the end of this function then it's guaranteed that there was no exception to re-raise, so the return value is irrelevant.

That will no longer be true with my suggested change to `TaskGroup.stop()`: we would get to the end of `__aexit__()` when a `CancelledError` is active that we don't want to re-raise. But I don't think you need a flag; since this is the only possible case, you can just unconditionally `return True`.

One other thing to consider is: what if we are already waiting at the end of the task group block (i.e., in the `__aexit__()` function in [`await self._on_completed_fut`](https://github.com/python/cpython/blob/2b2ad244c076588da401ffa68fc2cad346da6667/Lib/asyncio/taskgroups.py#L120))? In that case, cancelling the parent task is unnecessary, but it appears (from reading the code quickly) to be harmless since this loop is before the logic for un-cancelling the parent task, so it's probably simplest not to have any extra logic in stop() for that case.

## Comment 33 — arthur-tacca — 2024-11-18T09:47:30.000Z

There are two more possibilities to consider:

* `TaskGroup.stop()` is called on a task group that has already completed (its `async with` block has completed). You can tell this (as `TaskGroup.create_task()` does) with `self._exiting and not self._tasks`. There is nothing to do here, so it could simply return. Alternatively, it could raise a `RuntimeError`. Which is correct?
* `TaskGroup.stop()` is called on a task group that has not yet been entered. This is uncommon because usually you create a task group on the same line that you enter it, but it's possible to do that separately. Again, do we want to handle this? Or just raise a `RuntimeError`?

__It would be useful if any interested observers could give their opinions on these. Should they be allowed, or should they raise exceptions?__ (Personally, I'm neutral on both.)

---

It might seem like it would be very hard to get in these situations in the first place because surely it would be very obvious if `tg.stop()` is being called before or after the block? But, of course, it could be called from an async task:

```python
async def stop_sibling():
    async with TaskGroup() as outer_tg:
        inner_tg = TaskGroup()
        outer_tg.create_task(stop_it_later(inner_tg))
        async with inner_tg:
            inner_tg.create_task(foo())
            inner_tg.create_task(bar())
```

Depending on when `stop_it_later(inner_tg)` calls `inner_tg.stop()`, the inner task group may not have started or have already finished.

---

OK, so let's look at the implementation of these two cases. As I mentioned above, dealing with an already-closed task group is straightforward. But what about one that's not opened yet (assuming we don't just raise an error)?

We can't just cancel the parent task because we don't know what it is yet and it wouldn't be right to cancel it yet anyway.

There's no way to stop an `async def` block being entered at all from a context manager. So I think the best behaviour would be if `stop()` set a flag for this case, and `__aenter__()` checks it and, if set, cancels the parent task. That way, it will be cancelled at the first `await` within the task group body.

```python
def stop(self):
    if not self._entered:
        self._stop_on_enter = True  # raise RuntimeError(...)
    if self._exiting and not self._tasks:
        return  # Or: raise RuntimeError(...)
    if not self._aborting:
        self._abort()
        if not self._parent_cancel_requested:
            self._parent_cancel_requested = True
            self._parent_task.cancel()

async def __aenter__(self):
    # ... all the current stuff, up to self._entered = True ...

    if self._stop_on_enter:
        self.stop()

    return self
```

## Comment 34 — m-u-xyz — 2024-11-18T13:21:18.000Z

IMHO stopping a taskgroup that hasn't been entered yet shouldn't be possible in the first place. Taskgroups are created+entered with `async with TaskGroup() as taskgroup`. Anything else is a prime recipe for violating structured concurrency rules. So I'm somewhat in favor of raising a `RuntimeError` in that case.

Cancelling a taskgroup stops the tasks in it. If they're all stopped already _shrug_ this should be a no-op. I can't see why we'd want to raise an error in that case.

## Comment 35 — arthur-tacca — 2024-11-18T14:34:00.000Z

I know that asyncio is not Trio and vice-versa, but I still think it's worth noting its design in this area:

* It does not let you access a Nursery object until you enter a context manager (`trio.open_nursery()` returns something that can be turned into a Nursery rather than an actually nursery object).
* But arguably the closer analogue to `asyncio.TaskGroup` _in this particular discussion_ is actually `trio.CancelScope`, which you can construct directly and the [docs for it](https://trio.readthedocs.io/en/stable/reference-core.html#trio.CancelScope) even show constructing one separately in the very first code snippet.
* The docs say "If a cancel scope becomes cancelled before entering its with block, the [Cancelled](https://trio.readthedocs.io/en/stable/reference-core.html#trio.Cancelled) exception will be raised at the first checkpoint inside the with block. This allows a CancelScope to be created in one task and passed to another, so that the first task can later cancel some work inside the second."
* It doesn't directly say what happens if a scope is cancelled after it's already finished but a few places strongly imply that it's a no-op (except that it sets `cancel_called` to `True`), and I believe that to be the case.

Personally, I don't see how passing around an `asyncio.TaskGroup` before it has been entered purely for the sake of stopping it can violate structured concurrency, so on reflection I'd vote in favour of allowing stopping it before it's started. I'm tempted to say it would be good to raise an exception if it's already finished because there's no other way to tell, and users could always catch and suppress the exception if they don't care, but I'm still pretty neutral about that one.

## Comment 36 — m-u-xyz — 2024-11-18T15:09:54.000Z

> on reflection I'd vote in favour of allowing stopping it before it's started

Fine with me.

> I'm tempted to say it would be good to raise an exception if it's already finished because there's no other way to tell

Sure there is: wrap its context in a `try… finally: some_object.done=True` block.

There's also no built-in way to tell if a taskgroup is still running if you _don't_ want to cancel it. So either we add a flag on the taskgroup that can be interrogated, or we tell people to do the `try/finally` thing.

## Comment 37 — graingert — 2024-11-19T11:40:30.000Z

Contributor

If you stop a TaskGroup before it's entered that's like a cancelled CancelScope, sort of like:

```python
async with asyncio.timeout(-1):
    await asyncio.sleep(0)
    print("unreachable")
```

Or

```python
cs = anyio.CancelScope()
cs.cancel()
with cs:
    await anyio.sleep(0)
    print("unreachable")
```

But one event loop cycle faster?

## Comment 38 — belm0 — 2024-11-24T03:26:38.000Z

Contributor

[@arthur-tacca](https://github.com/arthur-tacca) regarding suppressing exception in __aexit__:

> But I don't think you need a flag; since this is the only possible case, you can just unconditionally return True.

Actually there is more to it, since if I append `return True` to the `if self._parent_task.uncancel() == 0` block, many asyncio tests fail.

I think it has to be the following, because other errors take precedence over Cancelled:

```python
    if self._parent_cancel_requested:
        # If this flag is set we *must* call uncancel().
        if self._parent_task.uncancel() == 0:
            # If there are no pending cancellations left,
            # don't propagate CancelledError.
            propagate_cancellation_error = None
            if not self._errors:
                return True
```

I'm noticing some ugly things in the `_aexit()` code, for example:

~~Why ever bother to set `propagate_cancellation_error` if there are other errors? This kind of misleading code confused us. It could be:~~

```python
if not self._errors and self._parent_cancel_requested and self._parent_task.uncancel() == 0:
    propagate_cancellation_error = None
    return True
```

(doesn't work, because as the comment says, uncancel() must always be called if _parent_cancel_requested is set)

## Comment 39 — arthur-tacca — 2024-11-24T08:15:21.000Z

Apologies for any confusion, I meant append `return True` to literally the very end of the `_aexit()`, at the top level (not inside any `if` blocks). Have you tried that? I must admit I haven't beyond inspecting the code. Alternatively, for the same effect, you could hardcode the return value in the `__aexit__()` function:

```python
async def __aexit__(self, et, exc, tb):
        tb = None
        try:
            await self._aexit(et, exc)
        finally:
            # ... All the finally stuff unchanged ...
        return True
```

As I explained in my comment, currently any exceptions that need to be raised are done with an explicit `raise` earlier in the function, even if it's just re-raising the original exception (and I don't propose to change that). So, if you took the existing taskgroups.py (without any of these changes for `stop()`) and just appended `return True` to the end, it should have no effect: it doesn't matter whether you suppress the exception if there's no exception to suppress. Adding the `stop()` method opens up this code path but only for the case that it's stopped and there's no other exceptions.

Again, I might've missed something, but that's what I was suggesting.

## Comment 40 — arthur-tacca — 2024-11-24T20:45:01.000Z

OK I finally did try out my idea for real today. I used the code I suggested above, added `self._stop_on_enter = False` in `__init__()`, and added a `return` statement at the end of the `if not self._entered` clause of `stop()` (oops!). Then it seemed to work, including passing the existing test suite and a couple of rudimentary ones that I put together. Here's one of them:

```python
async def test_taskgroup_stop_before_starting(self):
    should_not_get_here = False
    should_get_here = False
    tg = taskgroups.TaskGroup()
    tg.stop()
    self.assertEqual(asyncio.current_task().cancelling(), 0)
    async with tg:
        self.assertEqual(asyncio.current_task().cancelling(), 1)
        with self.assertRaises(RuntimeError):  # <-- interesting bit
            tg.create_task(asyncio.sleep(10))
        should_get_here = True
        await asyncio.sleep(0.01)
        should_not_get_here = True

    self.assertEqual(asyncio.current_task().cancelling(), 0)
    self.assertTrue(should_get_here)
    self.assertFalse(should_not_get_here)
```

This tests stopping the task group before it starts. As I've highlighted, one oddity is that you can't use `tg.create_task(...)` immediately after entering the task group, because it's already shutting down. On reflection, this makes sense, but it's certainly a bit surprising. Possibly a strike against allowing stopping a task group before it starts.

## Comment 41 — belm0 — 2024-11-24T21:28:27.000Z

Contributor

> you can't use tg.create_task(...) immediately after entering the task group, because it's already shutting down

That's sad-- would it be reasonable to stop the task group at enter rather than the first await? Maybe that isn't possible... then relax the RuntimeError on aborting in this case? The error seems questionable in general.

PR [#127214](https://github.com/python/cpython/pull/127214) is in progress

## Comment 42 — m-u-xyz — 2024-11-25T09:54:05.000Z

You can't stop the taskgroup "at enter" because an exception raised in `__aenter__` isn't passed through the corresponding `__aexit__`, thus we have no way to catch it.

While it may or may not be possible to fix that — notionally some special exception raised by `__enter__` could mean "behave as if the exception contained in me was raised immediately after entering the block normally", except I have no idea how that would work in conjunction with `ExitStack`), that's out of scope of this PR, IMHO.

## Comment 43 — arthur-tacca — 2024-11-26T01:51:32.000Z

> That's sad-- would it be reasonable to stop the task group at enter rather than the first await? Maybe that isn't possible... then relax the RuntimeError on aborting in this case? The error seems questionable in general.

smurfix addressed the first part of this. So what about suppressing the error in this case, or indeed always (if you find it questionable)?

Actually, this error is very useful. It provides the guarantee that a task group will never silently discard a request (though sadly that is only true if using [eager tasks](https://docs.python.org/3/library/asyncio-task.html#eager-task-factory) – see [#116048](https://github.com/python/cpython/issues/116048)). Then you can write code that consumes and reliably closes some resource, like a database connection:

```python
async def uses_connection(conn, work):
    try:
        await do_some_work(conn, work)
    finally:
        conn.close()

async def do_multiple():
    async with asyncio.TaskGroup() as tg:
        for i in range(my_count):
            conn = await create_connection()
            try:
                tg.create_task(uses_connection(conn, i))
            except RuntimeError:
                conn.close()
                raise
```

The above code should safely close the connection, regardless of what happens with cancellation and exceptions (again, so long as you're using eager tasks).

## Comment 44 — graingert — 2024-11-26T10:32:28.000Z

Contributor

`stop()` to me reminds me too much of `asyncio.EventLoop.stop`, it also implies that code after the TaskGroup won't run. how about: `TaskGroup.move_on()`

## Comment 45 — MarkofChina — 2024-11-27T12:42:14.000Z

why not to use：

```
async with asyncio.timeout（None）as tm：
    async with asyncio.TaskGroup() as tg：
             …
```

then：  
 tm.reschedule(0).  
 It felt more graceful.

## Comment 46 — graingert — 2024-11-27T12:54:06.000Z

Contributor

> why not to use：
>
> ```
> async with asyncio.timeout（None）as tm：
>     async with asyncio.TaskGroup() as tg：
>              …
> ```
>
> then： tm.reschedule(0). It felt more graceful.

This doesn't cancel the tasks soon enough, it waits for an event loop cycle, it raises a CancelledError that then needs to propagate out the TaskGroup body, and it raises a TimeoutError that needs careful management to suppresses correctly.

timeout could be fixed to cancel immediately when the deadline is exceeded (but you still need to wait for the cancellation to be raised and propagated), and have a exc=None kwarg to suppress the TimeputError

## Comment 47 — m-u-xyz — 2024-11-28T07:47:07.000Z

I have to admit that I'm strongly +1 on `.cancel` given that trio+anyio use that name. Trio is already an outlier in that its taskgroups are called "nurseries". We don't need even more different names for what's supposed to be the same thing.

IIRC Guido has objected to that because `.cancel` is supposed to raise a `CancelledError`. Well, it does do that. The fact that the exception will not propagate beyond the taskgroup it's been raised in is a feature; if you do want it to do that, you're free to expose the task's `cancel` method instead of the taskgroup's.

## Comment 48 — belm0 — 2024-11-30T00:22:05.000Z

Contributor

from ~~[@graingert](https://github.com/graingert)~~ [@arthur-tacca](https://github.com/arthur-tacca)

> It provides the guarantee that a task group will never silently discard a request

I don't agree this is necessary. If acknowledgement that a request was received is needed, it can be done at a higher level rather than relying on the task group API.

> Then you can write code that consumes and reliably closes some resource, like a database connection:

Maybe this example was contrived, but I didn't understand why the scope that created the connection isn't responsible for closing it (either via try/finally or a context manager).

## Comment 49 — belm0 — 2024-11-30T05:06:11.000Z

Contributor

As stated, calling create_task() on a task group that is cancelling yields a RuntimeError. I'll try making a case against this behavior.

Raising a generic RuntimeError usually implies you're doing something wrong-- like a logical error that cannot be resolved at runtime. It says "don't catch me explicitly" -- you'd have to parse the error string to do so, after all. While unlucky, I don't think it's wrong to call create_task() on a task group in cancelling state.

I view the following as an API inconsistency. Why is it OK (i.e. not a RuntimeError) to call create_task() on a task group that is essentially dead another way, namely within an expired timeout:

```python
async with timeout(0):
    async with asyncio.TaskGroup() as tg:
        tg.create_task(asyncio.sleep(1))  # not a RuntimeError
        # __aexit__ produces the expected TimeoutError
```

## Comment 50 — m-u-xyz — 2024-11-30T08:40:37.000Z

> Why is it OK (i.e. not a RuntimeError) to call create_task() on a task group that is essentially dead another way

IMHO the contract should be that _either_ the `create_task` fails, ideally with some non-generic `RuntimeError` subclass (`TaskgroupClosedError`?) which you can catch, _or_ the task starts to run (but may be cancelled the first time it `await`s something, assuming that the async call in question actually yields).

Anything else risks data loss.

Trio does the latter. For both compatibility and improved error handling (you only need to catch the problem at one point, inside the task, where you need to handle it _anyway_) I recommend following its example.

This example prints "Yes" before propagating the `RuntimeError`:

```
import trio

async def a(n):
    await trio.sleep(0.1)
    raise RuntimeError("This error is expected")

async def b(n):
    try:
        await trio.sleep(0.2)
    finally:
        n.start_soon(c)

async def c():
    print("Yes")
    await trio.sleep(0)
    raise RuntimeError("We don't go here")

async def main():
    async with trio.open_nursery() as n:
        n.start_soon(a,n)
        n.start_soon(b,n)
        pass

trio.run(main)
```

## Comment 51 — m-u-xyz — 2024-11-30T08:53:58.000Z

> Trio does the latter

as long as the taskgroup's `__aexit__`method has not returned, of course. After that it raises `RuntimeError`, which IMHO is reasonable at that point.

## Comment 52 — belm0 — 2024-11-30T22:51:35.000Z

Contributor

A problem with `create_task()` raising an error in the cancelled-before-enter case is that it makes the first `create_task()` call of any group special, and that call might not be written literally in the group's body. It could be called indirectly, by a helper function that spawns into a given TaskGroup instance. So then we'll need paranoia from any code potentially making such a call, as well as competing approaches: require the exception to be suppressed locally, vs. require something higher in the callback to be prepared for the exception.

I understand that when TaskGroup was first authored, this was an edge case that may not have been given much thought (especially prior to a feature of being able to cancel task groups), and the conservative thing to do was raise a runtime error. But raising an error (and especially as RuntimeError) is not clearly helpful in practice, and I hope this decision can be revisited.

## Comment 53 — arthur-tacca — 2024-12-01T23:58:59.000Z

[@belm0](https://github.com/belm0) You quoted me but attributed it to graingert.

For a fairly practical example of where a task group produces a resource and some tasks consume them, see [this example](https://github.com/python/cpython/issues/116048#issuecomment-1976270715) using `asyncio.start_server()` in the issue I linked earlier. I think you'd be hard pressed for a normal user of asyncio to notice the possible gap there.

By the way, the first call to `tg.create_task()` is not necessarily special (not that I agree with that characterisation anyway). If that call happens after an `await` (as happens in the example I just linked to) then the pending cancellation will be raised at that await and won't make it to `tg.create_task()`.

> Maybe this example was contrived, but I didn't understand why the scope that created the connection isn't responsible for closing it (either via try/finally or a context manager).

[@belm0](https://github.com/belm0) Sorry I didn't actually answer your question initially.

On the contrary, it would contrived if the connection closure didn't happen until the whole task group shuts down. If the connection sends a grateful shutdown request, or causes some fatal (for the connection) error, then it needs to be closed, and only the task representing the connection's lifetime would know to do that. The task group is for the whole length of time you can accept connections, which is usually much longer, potentially the whole lifetime of the server; if you wait for that to finish then you'll leak connections over time.

Side note 1: Yes, you could _also_ close connections when the task group shuts, as a backup for if the connection's task misses it. In fact I mentioned that in that issue (in the comment before). But you have to do some extra admin for that, and for no other reason than the missing run-till-await guarantee. As I said in that bug report: it looks suspiciously like a workaround for a bug because, IMO, it is.

Side note 2: This is really off topic, but actually a more careful application may well have a different scope for connection creation and connection cleanup. That way, when the overall server shuts down, it can immediately stop accepting new connections (cancel listener task group immediately) while still giving some some time to allow gracefully shutting down existing connections (so, maybe surprisingly, the connection task group is _outside_ the listener task group).

## Comment 54 — rrevans — 2024-12-27T23:46:50.000Z

Many of the use cases mentioned above can be implemented without `TaskGroup.cancel`? Examples below.

Also maybe I'm misunderstanding, but it seems all cases could be implemented by wrapping the group in a `Task` and cancelling that?

###### Race-to-the-finish with two calls (e.g. happy eyeballs use case)

```python
await with asyncio.TaskGroup() as g:
  t1 = g.create_task(...)
  t2 = g.create_task(...)
  t1.add_done_callback(lambda _: t2.cancel())
  t2.add_done_callback(lambda _: t1.cancel())
```

Or `await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)` then cancel the still-pending tasks.

###### Explicit stop signal sent to a task group

```python
stop = asyncio.Event()
await with asyncio.TaskGroup() as g:
  # create some tasks
  await stop.wait()
  raise asyncio.CancelledError()
```

Call `stop.set()` elsewhere to stop the `TaskGroup`.

###### Tracking tasks from `asyncio.start_server` for eager shutdown

```python
async def handle_connection(r, w):
  ...
await with asyncio.TaskGroup() as g:
  def connect_cb(r, w):
    g.create_task(handle_connection(r, w))
  s = await asyncio.start_server(connect_cb, ...)
  g.create_task(s.serve_forever())
```

Run this as a coroutine or task, and cancel propagates automatically to server and all active connections.

## Comment 55 — m-u-xyz — 2024-12-30T07:43:08.000Z

> Also maybe I'm misunderstanding, but it seems all cases could be implemented by wrapping the group in a Task and cancelling that?

Well. Possibly. However …

> then cancel the still-pending tasks

… unraveling thread spaghetti is exactly what taskgroups are supposed to shield the programmer from.

Also, when you cancel the task you preclude it from returning a result. "Return a value by assigning it to some variable in an outer scope" (or to an object's attribute) is an anti-pattern I'd like to be able to avoid.

## Comment 56 — rrevans — 2024-12-31T06:03:29.000Z

Maybe this is really about avoiding separate functions / objects for the state? I agree there's merit to keeping that wiring together in one scope to avoid clumsy decomposition into separate tasks.

__So then the main goal becomes creating a _cancellable scope_ within a function.__

I think it's helpful to consider the semantics such a cancellable scope should have separately from the possibility of integrating it into the existing `TaskGroup`. Such a scope could enclose a `TaskGroup` after if they are separate.

For exposition, suppose the parts of `TaskGroup` and `timeout` that deal with cancelling the inner suite of the `async with` statement could be factored out to a separate (synchronous) `CancelContext()` context manager type.

```python
with CancelContext() as ctx:
  SUITE
```

`CancelContext.cancel()` cancels the inner suite. Execution continues after the `with`-statement entering the cancelled scope unless the suite raises an exception (for clarity, `CancelledError` is suppressed). Callers can detect normal vs. cancel exit with `try`-`else` or `CancelContext` can expose a `CancelContext.cancelled` property.

An as example of using `CancelContext`, the config reload case then becomes:

```python
async def subsys(config):
  while True:
    with CancelContext() as ctx:
      async with TaskGroup() as tg:
        tg.create_task(wait_changed(ctx.cancel, config))
        ... # now do whatever the config tells us to do
```

or factored out as a context manager for reuse:

```python
@contextlib.asynccontextmanager
async def monitor(config):
  with CancelContext() as ctx:
    async with asyncio.TaskGroup() as tg:
      tg.create_task(wait_changed(ctx.cancel, config))
      yield

async def subsys(config):
  while True:
    async with monitor(config):
      ... # now do whatever the config tells us to do
```

Note that `CancelContext` is almost equivalent to running the statements of the inner suite in a separate task with `cancel()` implemented by cancelling the task and suppressing the `CancelledError`:

| `CancelContext` | Separate `Task` |
| --- | --- |
| ``` with CancelContext() as ctx:   something.add_callback(ctx.cancel)   async with asyncio.TaskGroup() as tg:     ... do some stuff ``` | ``` async def do_stuff(...):   async with asyncio.TaskGroup() as tg:     ... do some stuff  async with asyncio.TaskGroup() as tg:   t = tg.create_task(do_stuff(...))   something.add_callback(t.cancel) ``` |

With this new type, `TaskGroup.cancel()` as imagined so far is semantically identical to the sample above wrapping `TaskGroup` in a `CancelContext` and calling `CancelContext.cancel()`.

This also means uses of `TaskGroup.cancel()` should be able to be transformed to a separate task with almost the same semantics, but the goal should be a cancellable scope to avoid the required boilerplate.

Some other possible variations of `CancelContext`:

* `CancelContext.cancel()` could instead raise `CancelledError`
* `CancelContext.throw(exc)` could raise a user-provided exception once the inner suite is cancelled

Coming back around to `TaskGroup`, its main responsibilities are:

* guarantee all tasks are finished upon context exit
* propagate all exceptions raised other than tasks raising `CancelledError`
* cancel all tasks and the suite if and only if an exception is propagated

These are already complex, so adding more should be done very carefully and ideally match the those somewhat closely. So what about `CancelContext` and its variations?

* `CancelContext.cancel()` silently exiting the suite is surprising and does not match any exit behavior of `TaskGroup`.

  * `TaskGroup` never cancels tasks or the suite for a normal exit
  * `TaskGroup` never exits the suite normally when cancelling tasks
  * `CancelContext.cancel()` acts neither like `task.cancel()` nor the suite raising `CancelledError`
  * Also in general silently exiting would be subtle and error-prone.
* `CancelContext.cancel()` raising `CancelledError` is consistent with non-normal exit always raising an exception for `TaskGroup`, but existing `TaskGroup` does not raise `CancelledError` except when the enclosing scope is cancelled.

  * Raising `CancelledError` would be a tricky pitfall for unconditionally swallowing cancel vs. external cancel
* `CancelContext.throw(exc)` throwing a user-defined exception is quite close to the existing semantics

  * It is almost the same as `TaskGroup.create_task()` with a coroutine that unconditionally raises the exception. But then passing `CancelledError` would be suppressed, so probably this should be treated as-if the _suite_ raised it.
  * The raised exception would probably be added unconditionally to the resulting `ExceptionGroup`

Meanwhile `CancelContext` semantics seem useful enough for implementing context managers that need to cancel an inner suite, and it can be implemented without access to `TaskGroup` internals.

As an example, consider the config reload sample as a context manager. `config.watch()` would await the config to be ready initially and cancel the inner suite when reloaded.

```python
class Config:
  @contextlib.asynccontextmanager
  async def watch(self):
    await self.ready()
    with CancelContext() as c:
      self.add_callback(c.cancel)
      yield self

async def subsys(config):
  while True:
    async with config.watch():
      ... # now do whatever the config tells us to do
```

Parting observations:

* `CancelContext` could be a model for a low-level API exposed by `Task` itself to properly scope nested cancel operations and clean up the subtle `Task.cancel()`, `Task.cancelling()`, `Task.uncancel()` dance once and for all.
* `CancelContext.throw(exc)` also seems maybe useful for addressing the problems that motivate [Asyncio: Send arbitrary exceptions to Tasks #102847](https://github.com/python/cpython/issues/102847).

Okay this got long so I'll try to summarize the main points:

* the main value here seems to be to define a cancellable scope rather than a stoppable `TaskGroup`
* it seems useful to reason about cancellable scope semantics separately from `TaskGroup` details
* it seems like a separate question whether `TaskGroup` and `CancelContext` should be the same
* _silently_ exiting the suite when cancelled seems quite error prone and subtle
* `CancelContext.throw(exc)` almost fits `TaskGroup` except its special-casing of `CancelledError`

## Comment 57 — graingert — 2024-12-31T06:40:44.000Z

Contributor

I think we should keep discussions of what sort of name/arguments .cancel() should have here, but move whether the feature is useful or not back to discuss: <https://discuss.python.org/t/how-to-cancel-all-tasks-created-within-same-taskgroup/30215>

## Comment 58 — m-u-xyz — 2024-12-31T09:37:56.000Z

> Okay this got long so I'll try to summarize the main points:

… or you could compare with Trio's (and anyio's) `CancelScope` which already do this.

Personally I don't particularly care whether these objects are a separate class, or simply a `Taskgroup` that happens not to have subtasks.

The problem is that if you create a separate cancellable scope, you run into a fundamental disparity between cancellation as it currently works on asyncio (it's a singular exception, for the most part) and Trio/anyio (it's a state of the scope: any current _or_ subsequent async call might raise/re-raise the exception, which is why Trio introduced a shielding `CancelScope` that blocks this – essential in `finally:` blocks that need to do async calls).

`anyio`'s `CancelScope`s basically work like Trio's. It goes to some lengths to paper over this disparity. ("Task still running? Well, cancel it again.") I'd expect some minor(?) compatibility problems if asyncio were to switch its paradigm.

## Comment 59 — rrevans — 2025-01-01T13:48:03.000Z

> we should keep discussions of what sort of name/arguments .cancel() should have here

In my opinion:

`TaskGroup.cancel()` should take no arguments and cause `TaskGroup.__aexit__` to raise `CancelledError` after cancelling the suite and its tasks.

`TaskGroup.cancel()` should have no effect at all if the `TaskGroup` has already exited or if the suite or any task raises an exception other than `CancelledError` (in which case an `ExceptionGroup` is raised instead).

When catching the `CancelledError`, callers must (as always) check `current_task().cancelling()` before swallowing the exception if the parent task is itself subject to cancellation.

This behavior is the same as arranging for the suite to raise `CancelledError` upon request except that the suite can execute other statements.

## Comment 60 — arthur-tacca — 2025-01-01T22:40:35.000Z

[@rrevans](https://github.com/rrevans) Perhaps this is just a language thing, but to me the tone of your most recent comment (with no "in my view" or "I would do it like this" etc.) suggests you're trying to summarise consensus. But what you've written is definitely not that. As far as I can tell, you're the only person to suggest that `TaskGroup.cancel()` should be allowed to propagate out of the context manager. That would defeat the point of the feature!

About your previous comment: As [@smurfix](https://github.com/smurfix) said, you've rediscovered Trio's cancellation scopes, which are non-async context managers. I think it's a pity that asyncio didn't copy their design in task groups (and for timeouts) but that ship has sailed and there's no point going back over that now. The same goes for edge-based vs level-based cancellation (which Guido has explicitly said will never be in asyncio).

One thing occurred to me reading the bit of your comment about injecting a different cancellation exception: I think the problem you're actually trying to solve is detected why a cancellation occurred (was it because this task group / cancellation scope was manually cancelled?). Trio actually has a way to do this that would be really easy to copy here: the [`cancel_called`](https://trio.readthedocs.io/en/stable/reference-core.html#trio.CancelScope.cancelled_called) and [`cancel_caught`](https://trio.readthedocs.io/en/stable/reference-core.html#trio.CancelScope.cancelled_caught) attributes. You could use it like this:

```python
with trio.CancelScope() as cs:
    ...
    if something():
        cs.cancel()
    ...
if cs.cancel_caught:
    ...
```

(It feels more natural to use `cancel_called`, and in this snippet it probably wouldn't make any difference, but the docs say it's usually better to `cancel_caught` instead. They can differ if an outside cancellation happens at roughly the same time, which causes cancellation to propagate or rather than be caught, or if you cancel the scope after it's already closed.)

It's this possible for the application developer to wrap up this functionality in a context manager, which can re-raise a different exception if desired. Actually, that's exactly how `trio.fail_after` works. These facilities are also available on nurseries (Trio task groups) because they include a cancel scope as an attribute.

As a side note, you can already do something similar in asyncio already too, using `asyncio.timeout`:

```python
try:
    with asyncio.timeout(None) as to:
        ...
        if something():
            to.reschedule(asyncio.current_loop().time() - 1)
        ...
except TimeoutError
    ...
```

A disadvantage of this approach is that there's no way to tell if the `TimeoutError` came from this score or some inner one. Another disadvantage is that it's clearly a really indirect and confusing usage of the API.

I'll cross-post the attribute idea on the Discuss topic too.

## Comment 61 — rrevans — 2025-01-02T15:56:33.000Z

> propagate out of the context manager

`TaskGroup` raises an exception in all cases where it exits before successfully completing the suite and its tasks. I think `.cancel()` should also raise an exception because it has the same effect.

Why not `.stop()` and exit silently? Because `asyncio` and python at large prefer exceptions to communicate unusual conditions. I think most of the time callers will need to respond to cancel-exit as an unexpected outcome, so explicitly raising an exception is clear, natural, and consistent.

Why `CancelledError`? Because it matches `Task.cancel()`. Both would effectively raise `CancelledError` from the body of the thing being cancelled. This is the coroutine for `Task` and the with-statement for `TaskGroup`.

Callers might inadvertently swallow the exception if the parent is being cancelled, but I believe the risk of bad side-effects from that programming error are less severe that the risks of proceeding inadvertently past a cancelled `TaskGroup`.

> would defeat the point of the feature

Maybe. My take is that the semantics of `TaskGroup.cancel()` should closely follow the behavior of `Task.cancel()` rather than trying to emulate Trio cancellation scopes.

## Comment 62 — gvanrossum — 2025-01-02T23:39:34.000Z

Member

I may be mistaken, but it appears this discussion is going around in circles. Why exactly was the title changed from `stop` to `cancel`? It seems we're not even on the same page about whether the desired API should exit the `async with` clause without error (if all tasks were cancelled without errors) or not.

Maybe it's time for the key participants to start collaborating on a PEP? If you can get a draft PEP ready and rough consensus around it I am willing to sponsoring it. I am proposing this as an alternative to continuing the debate without making progress for another 6 months.

One suggestion for a draft PEP: make minimal use of references to Trio. We can't expect the ultimate deciders (the SC; and before them myself or whichever core dev ends up sponsoring) to understand much of the ins and outs of Trio, so motivations of the form "this is how Trio does it" are not helpful for the deciders (especially since this appears largely about cancellation, and that's an are where asyncio and Trio differ fundamentally).

You're also all welcome to keep arguing like you have been, but there has to be some kind of consensus. Currently the discussion is hard to follow unless you have read the _entire_ issue, which is unreasonable at this point.

## Comment 63 — belm0 — 2025-01-04T01:14:01.000Z

Contributor

> Why exactly was the title changed from `stop` to `cancel`?

[Yury balked at stop() in the PR](https://github.com/python/cpython/pull/127214#pullrequestreview-2459521017), so I changed it back to cancel().

Though I suggested starting a PEP when asking for the issue to be reopened, I wasn't imagining this level of controversy 😓

## Comment 64 — gvanrossum — 2025-01-04T01:46:20.000Z

Member

> > Why exactly was the title changed from `stop` to `cancel`?
>
> [Yury balked at stop() in the PR](https://github.com/python/cpython/pull/127214#pullrequestreview-2459521017), so I changed it back.

But he was just asking why — not telling you to change it. You could maybe have answered “because this design doesn’t raise CancelledError out of the `asynchronous with` block”?

## Comment 65 — kumaraditya303 — 2025-01-04T02:21:10.000Z

Contributor

> One suggestion for a draft PEP: make minimal use of references to Trio. We can't expect the ultimate deciders (the SC; and before them myself or whichever core dev ends up sponsoring) to understand much of the ins and outs of Trio, so motivations of the form "this is how Trio does it" are not helpful for the deciders (especially since this appears largely about cancellation, and that's an are where asyncio and Trio differ fundamentally).

I agree with Guido, fwiw this is the reason I don't participate much in these discussions because to me it feels more about making asyncio behave like trio or anyio etc and I don't understand much of those projects. Both projects are fundamentally different in many regards for that to happen so IMO it would be better to propose concrete semantics in terms of how things happen in asyncio rather than any other such projects.

## Comment 66 — gvanrossum — 2025-01-04T02:30:27.000Z

Member

> Though I suggested starting a PEP when asking for the issue to be reopened, I wasn't imagining this level of controversy 😓

It seems there are at least two dimensions of controversy, and it also seems we're going back and forth on at least one?

* Whether it should raise CancelledError or exits cleanly after all tasks are cancelled cleanly (and from the outcome of this I would derive a preference for the name -- stop() if it exits cleanly, cancel() if it raises CancelledError -- though I see in he PR that you prefer cancel() regardless; nevertheless we should decide whether it raises CancelledError or not first, IMO).
* Whether the use case is important enough to add such an API at all. Connected to this is whether it can be done without too much fuss and with the correct semantics using the existing APIs; or by making an existing internal API public, which would argue that it's not _really_ adding new functionality. But it appears we have conclusively proven that we can't just promote an existing API, and the only correct way using existing APIs seems to be by adding a dummy task that raises a unique exception, and catching that, which is hardly clean. You also appear to have shown (by looking at the popularity of the corresponding Trio API) that the use case is important -- although I'd like to see at least a few of those Trio uses for myself to understand how it's used in Trio and whether we can really draw conclusions for asyncio from that.

Maybe you can try to address these bullets separately and drive consensus that way (instead of by drafting a PEP)? You'd have to go to battle a bit with the faction (or one person?) who want the `async with` block to raise CanceledError. Does that desire come largely out of the desire to name the API cancel()? Or is there a practical reason, like making the coding of various realistic-sounding examples more convenient? Arguments from a pure desire of consistency are sometimes red herrings.

Finally: If I'm the only person here who believes that if the `async with` block can exit cleanly, then the API should be called `stop()`, I'll give up on that.

## Comment 67 — m-u-xyz — 2025-01-04T09:28:50.000Z

> I'd like to see at least a few of those Trio uses for myself

The canonical example is Happy Eyeballs-style concurrency, i.e. start a few tasks that all try to produce a result, the first one that succeeds wins, all the others get cancelled, you return the result of the winning task.

This is easy enough with a taskgroup that doesn't raise an exception when getting cancelled. If you don't have that, you need a wrapper to catch the exception. That wrapper needs to distinguish "the taskgroup has been (self-)cancelled" from "the cancellation was triggered externally and thus must propagate". That's boilerplate which people can and will get wrong. (Consider what happens when, due to scheduling quirks, these coincide …)

Another example: a network connection. When you decide to disconnect, you self-cancel the whole connection handling taskgroup (sometimes these are quite complex: send task, receive task, background ping task …) and you're done. Why would you propagate that cancellation? Worse, if the network connection was to retrieve a result from the other side, again you need that wrapper.

I'd ask the other way 'round: what's the usecase of propagating that cancellation? Because frankly I don't see one, esp. given that you can do it that way simply by cancelling the current task instead.

## Comment 68 — rmorshea — 2025-06-30T20:58:13.000Z

> Whether the use case is important enough to add such an API at all.

Anecdotally, at my current employer we have a lot of hardware drivers that do something similar to the following:

```python
class MyDriver:

    async def arm(self):
        self._background_tasks = []
        self._background_tasks.append(asyncio.create_task(...))
        self._background_tasks.append(asyncio.create_task(...))
        self._background_tasks.append(asyncio.create_task(...))

    async def disarm(self):
        for t in self._background_tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
               await t
```

People don't seem to use task groups because they aren't providing any ergonomic benefit - you still have to manually track all the background tasks:

```python
class MyDriver:

    async def arm(self):
        self._background_tasks = []
        self._context = AsyncExitStack()
        self._task_group = await self._context.enter_async_context(asyncio.TaskGroup())
        self._background_tasks.append(self._task_group.create_task(...))
        self._background_tasks.append(self._task_group.create_task(...))
        self._background_tasks.append(self._task_group.create_task(...))

    async def disarm(self):
        for t in self._background_tasks:
            t.cancel()
        await self._context.aclose()
```

However, if there were a `TaskGroup.cancel` method that didn't raise (or could optionally) it'd be great to do:

```python
class MyDriver:

    async def arm(self):
        self._context = AsyncExitStack()
        self._task_group = await self._context.enter_async_context(asyncio.TaskGroup())
        self._task_group.create_task(...)
        self._task_group.create_task(...)
        self._task_group.create_task(...)

    async def disarm(self):
        self._task_group.cancel()
        await self._context.aclose()
```

## Comment 69 — agronholm — 2025-07-01T14:13:22.000Z

Contributor

How do you feel about this API? [agronholm/anyio#890](https://github.com/agronholm/anyio/pull/890)

## Comment 70 — agronholm — 2025-07-01T14:15:56.000Z

Contributor

Anyways, with the `master` version of AnyIO, you would write the code like this:

```python
from anyio import AsyncContextManagerMixin, create_task_group

class MyDriver(AsyncContextManagerMixin):

    @asynccontextmanager
    async def __asynccontextmanager__():
        async with create_task_group() as tg:
            tg.start_soon(...)
            tg.start_soon(...)
            tg.start_soon(...)
            yield
            tg.cancel_scope.cancel()
```

This, of course, implies that you would use this class as an async context manager.
