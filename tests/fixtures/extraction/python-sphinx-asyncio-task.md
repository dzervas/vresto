Theme 

### [Table of Contents](https://docs.python.org/3/contents.html)

* [Coroutines and tasks](https://docs.python.org/3/library/asyncio-task.html)
  * [Coroutines](https://docs.python.org/3/library/asyncio-task.html#coroutines)
  * [Awaitables](https://docs.python.org/3/library/asyncio-task.html#awaitables)
  * [Creating tasks](https://docs.python.org/3/library/asyncio-task.html#creating-tasks)
  * [Task cancellation](https://docs.python.org/3/library/asyncio-task.html#task-cancellation)
  * [Task groups](https://docs.python.org/3/library/asyncio-task.html#task-groups)
  * [Sleeping](https://docs.python.org/3/library/asyncio-task.html#sleeping)
  * [Running tasks concurrently](https://docs.python.org/3/library/asyncio-task.html#running-tasks-concurrently)
  * [Eager task factory](https://docs.python.org/3/library/asyncio-task.html#eager-task-factory)
  * [Shielding from cancellation](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation)
  * [Timeouts](https://docs.python.org/3/library/asyncio-task.html#timeouts)
  * [Waiting primitives](https://docs.python.org/3/library/asyncio-task.html#waiting-primitives)
  * [Running in threads](https://docs.python.org/3/library/asyncio-task.html#running-in-threads)
  * [Scheduling from other threads](https://docs.python.org/3/library/asyncio-task.html#scheduling-from-other-threads)
  * [Introspection](https://docs.python.org/3/library/asyncio-task.html#introspection)
  * [Task object](https://docs.python.org/3/library/asyncio-task.html#task-object)

#### Previous topic

[Runners](https://docs.python.org/3/library/asyncio-runner.html "previous chapter")

#### Next topic

[Streams](https://docs.python.org/3/library/asyncio-stream.html "next chapter")

### This page

* [Report a bug](https://docs.python.org/3/bugs.html)
* [Improve this page](https://docs.python.org/3/improve-page.html?pagetitle=Coroutines+and+tasks&pageurl=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio-task.html&pagesource=library%2Fasyncio-task.rst)
* [Show source](https://github.com/python/cpython/blob/3.15/Doc/library/asyncio-task.rst?plain=1)

### Navigation

* [index](https://docs.python.org/3/genindex.html "General Index")
* [modules](https://docs.python.org/3/py-modindex.html "Python Module Index") |
* [next](https://docs.python.org/3/library/asyncio-stream.html "Streams") |
* [previous](https://docs.python.org/3/library/asyncio-runner.html "Runners") |
* ![Python logo](https://docs.python.org/3/_static/py.svg)
* [Python](https://www.python.org/) »

* [3.15.0 Documentation](https://docs.python.org/3/index.html) »
* [The Python standard library](https://docs.python.org/3/library/index.html) »
* [Networking and Interprocess Communication](https://docs.python.org/3/library/ipc.html) »
* [`asyncio` — Asynchronous I/O](https://docs.python.org/3/library/asyncio.html) »
* [Coroutines and tasks](https://docs.python.org/3/library/asyncio-task.html)
* |
* Theme  |

# Coroutines and tasks

This section outlines high-level asyncio APIs to work with coroutines and Tasks.

* [Coroutines](https://docs.python.org/3/library/asyncio-task.html#coroutines)
* [Awaitables](https://docs.python.org/3/library/asyncio-task.html#awaitables)
* [Creating tasks](https://docs.python.org/3/library/asyncio-task.html#creating-tasks)
* [Task cancellation](https://docs.python.org/3/library/asyncio-task.html#task-cancellation)
* [Task groups](https://docs.python.org/3/library/asyncio-task.html#task-groups)
* [Sleeping](https://docs.python.org/3/library/asyncio-task.html#sleeping)
* [Running tasks concurrently](https://docs.python.org/3/library/asyncio-task.html#running-tasks-concurrently)
* [Eager task factory](https://docs.python.org/3/library/asyncio-task.html#eager-task-factory)
* [Shielding from cancellation](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation)
* [Timeouts](https://docs.python.org/3/library/asyncio-task.html#timeouts)
* [Waiting primitives](https://docs.python.org/3/library/asyncio-task.html#waiting-primitives)
* [Running in threads](https://docs.python.org/3/library/asyncio-task.html#running-in-threads)
* [Scheduling from other threads](https://docs.python.org/3/library/asyncio-task.html#scheduling-from-other-threads)
* [Introspection](https://docs.python.org/3/library/asyncio-task.html#introspection)
* [Task object](https://docs.python.org/3/library/asyncio-task.html#task-object)

## [Coroutines](https://docs.python.org/3/library/asyncio-task.html#id2)

__Source code:__ [Lib/asyncio/coroutines.py](https://github.com/python/cpython/tree/3.15/Lib/asyncio/coroutines.py)

---

[Coroutines](https://docs.python.org/3/glossary.html#term-coroutine) declared with the async/await syntax is the preferred way of writing asyncio applications. For example, the following snippet of code prints “hello”, waits 1 second, and then prints “world”:

```
>>> import asyncio

>>> async def main():
...     print('hello')
...     await asyncio.sleep(1)
...     print('world')

>>> asyncio.run(main())
hello
world
```

Note that simply calling a coroutine will not schedule it to be executed:

```
>>> main()
<coroutine object main at 0x1053bb7c8>
```

To actually run a coroutine, asyncio provides the following mechanisms:

* The [`asyncio.run()`](https://docs.python.org/3/library/asyncio-runner.html#asyncio.run "asyncio.run") function to run the top-level entry point “main()” function (see the above example.)
* Awaiting on a coroutine. The following snippet of code will print “hello” after waiting for 1 second, and then print “world” after waiting for _another_ 2 seconds:

  ```
  import asyncio
  import time

  async def say_after(delay, what):
      await asyncio.sleep(delay)
      print(what)

  async def main():
      print(f"started at {time.strftime('%X')}")

      await say_after(1, 'hello')
      await say_after(2, 'world')

      print(f"finished at {time.strftime('%X')}")

  asyncio.run(main())
  ```

  Expected output:

  ```
  started at 17:13:52
  hello
  world
  finished at 17:13:55
  ```
* The [`asyncio.create_task()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task "asyncio.create_task") function to run coroutines concurrently as asyncio [`Tasks`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task").

  Let’s modify the above example and run two `say_after` coroutines _concurrently_:

  ```
  async def main():
      task1 = asyncio.create_task(
          say_after(1, 'hello'))

      task2 = asyncio.create_task(
          say_after(2, 'world'))

      print(f"started at {time.strftime('%X')}")

      # Wait until both tasks are completed (should take
      # around 2 seconds.)
      await task1
      await task2

      print(f"finished at {time.strftime('%X')}")
  ```

  Note that expected output now shows that the snippet runs 1 second faster than before:

  ```
  started at 17:14:32
  hello
  world
  finished at 17:14:34
  ```
* The [`asyncio.TaskGroup`](https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup "asyncio.TaskGroup") class provides a more modern alternative to [`create_task()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task "asyncio.create_task"). Using this API, the last example becomes:

  ```
  async def main():
      async with asyncio.TaskGroup() as tg:
          task1 = tg.create_task(
              say_after(1, 'hello'))

          task2 = tg.create_task(
              say_after(2, 'world'))

          print(f"started at {time.strftime('%X')}")

      # The await is implicit when the context manager exits.

      print(f"finished at {time.strftime('%X')}")
  ```

  The timing and output should be the same as for the previous version.

  Added in version 3.11: [`asyncio.TaskGroup`](https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup "asyncio.TaskGroup").

## [Awaitables](https://docs.python.org/3/library/asyncio-task.html#id3)

We say that an object is an __awaitable__ object if it can be used in an [`await`](https://docs.python.org/3/reference/expressions.html#await) expression. Many asyncio APIs are designed to accept awaitables.

There are three main types of _awaitable_ objects: __coroutines__, __Tasks__, and __Futures__.

Coroutines

Python coroutines are _awaitables_ and therefore can be awaited from other coroutines:

```
import asyncio

async def nested():
    return 42

async def main():
    # Nothing happens if we just call "nested()".
    # A coroutine object is created but not awaited,
    # so it *won't run at all*.
    nested()  # will raise a "RuntimeWarning".

    # Let's do it differently now and await it:
    print(await nested())  # will print "42".

asyncio.run(main())
```

Important

In this documentation the term “coroutine” can be used for two closely related concepts:

* a _coroutine function_: an [`async def`](https://docs.python.org/3/reference/compound_stmts.html#async-def) function;
* a _coroutine object_: an object returned by calling a _coroutine function_.

Tasks

_Tasks_ are used to schedule coroutines _concurrently_.

When a coroutine is wrapped into a _Task_ with functions like [`asyncio.create_task()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task "asyncio.create_task") the coroutine is automatically scheduled to run soon:

```
import asyncio

async def nested():
    return 42

async def main():
    # Schedule nested() to run soon concurrently
    # with "main()".
    task = asyncio.create_task(nested())

    # "task" can now be used to cancel "nested()", or
    # can simply be awaited to wait until it is complete:
    await task

asyncio.run(main())
```

Futures

A [`Future`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future "asyncio.Future") is a special __low-level__ awaitable object that represents an __eventual result__ of an asynchronous operation.

When a Future object is _awaited_ it means that the coroutine will wait until the Future is resolved in some other place.

Future objects in asyncio are needed to allow callback-based code to be used with async/await.

Normally __there is no need__ to create Future objects at the application level code.

Future objects, sometimes exposed by libraries and some asyncio APIs, can be awaited:

```
async def main():
    await function_that_returns_a_future_object()

    # this is also valid:
    await asyncio.gather(
        function_that_returns_a_future_object(),
        some_python_coroutine()
    )
```

A good example of a low-level function that returns a Future object is [`loop.run_in_executor()`](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.run_in_executor "asyncio.loop.run_in_executor").

## [Creating tasks](https://docs.python.org/3/library/asyncio-task.html#id4)

__Source code:__ [Lib/asyncio/tasks.py](https://github.com/python/cpython/tree/3.15/Lib/asyncio/tasks.py)

---

### asyncio.create_task(_coro_, _*_, _name=None_, _context=None_, _eager_start=None_, _**kwargs_)

Wrap the _coro_ [coroutine](https://docs.python.org/3/library/asyncio-task.html#coroutine) into a [`Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task") and schedule its execution. Return the Task object.

The full function signature is largely the same as that of the [`Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task") constructor (or factory) - all of the keyword arguments to this function are passed through to that interface.

An optional keyword-only _context_ argument allows specifying a custom [`contextvars.Context`](https://docs.python.org/3/library/contextvars.html#contextvars.Context "contextvars.Context") for the _coro_ to run in. The current context copy is created when no _context_ is provided.

An optional keyword-only _eager_start_ argument allows specifying if the task should execute eagerly during the call to create_task, or be scheduled later. If _eager_start_ is not passed the mode set by [`loop.set_task_factory()`](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.set_task_factory "asyncio.loop.set_task_factory") will be used.

The task is executed in the loop returned by [`get_running_loop()`](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.get_running_loop "asyncio.get_running_loop"), [`RuntimeError`](https://docs.python.org/3/builtins/exceptions.html#RuntimeError "RuntimeError") is raised if there is no running loop in current thread.

Note

[`asyncio.TaskGroup.create_task()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup.create_task "asyncio.TaskGroup.create_task") is a new alternative leveraging structural concurrency; it allows for waiting for a group of related tasks with strong safety guarantees.

Important

Save a reference to the result of this function, to avoid a task disappearing mid-execution. The event loop only keeps weak references to tasks. A task that isn’t referenced elsewhere may get garbage collected at any time, even before it’s done. For reliable “fire-and-forget” background tasks, gather them in a collection:

```
background_tasks = set()

for i in range(10):
    task = asyncio.create_task(some_coro(param=i))

    # Add task to the set. This creates a strong reference.
    background_tasks.add(task)

    # To prevent keeping references to finished tasks forever,
    # make each task remove its own reference from the set after
    # completion:
    task.add_done_callback(background_tasks.discard)
```

Note that this approach never awaits the tasks, so if a task fails, its exception is never retrieved and asyncio logs a “Task exception was never retrieved” message when the task is garbage collected. To avoid this, use [`asyncio.TaskGroup`](https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup "asyncio.TaskGroup") which keeps a strong reference to each task, awaits them and propagates their exceptions:

```
async with asyncio.TaskGroup() as tg:
    for i in range(10):
        tg.create_task(some_coro(param=i))
```

Added in version 3.7.

Changed in version 3.8: Added the _name_ parameter.

Changed in version 3.11: Added the _context_ parameter.

Changed in version 3.14: Added the _eager_start_ parameter by passing on all _kwargs_.

## [Task cancellation](https://docs.python.org/3/library/asyncio-task.html#id5)

Tasks can easily and safely be cancelled. When a task is cancelled, [`asyncio.CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") will be raised in the task at the next opportunity.

It is recommended that coroutines use `try/finally` blocks to robustly perform clean-up logic. In case [`asyncio.CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") is explicitly caught, it should generally be propagated when clean-up is complete. `asyncio.CancelledError` directly subclasses [`BaseException`](https://docs.python.org/3/builtins/exceptions.html#BaseException "BaseException") so most code will not need to be aware of it.

The asyncio components that enable structured concurrency, like [`asyncio.TaskGroup`](https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup "asyncio.TaskGroup") and [`asyncio.timeout()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout "asyncio.timeout"), are implemented using cancellation internally and might misbehave if a coroutine swallows [`asyncio.CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError"). Similarly, user code should not generally call [`uncancel`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.uncancel "asyncio.Task.uncancel"). However, in cases when suppressing `asyncio.CancelledError` is truly desired, it is necessary to also call `uncancel()` to completely remove the cancellation state.

## [Task groups](https://docs.python.org/3/library/asyncio-task.html#id6)

Task groups combine a task creation API with a convenient and reliable way to wait for all tasks in the group to finish.

### _class_ asyncio.TaskGroup

An [asynchronous context manager](https://docs.python.org/3/reference/datamodel.html#async-context-managers) holding a group of tasks. Tasks can be added to the group using [`create_task()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task "asyncio.create_task"). All tasks are awaited when the context manager exits.

Added in version 3.11.

#### create_task(_coro_, _*_, _name=None_, _context=None_, _eager_start=None_, _**kwargs_)

Create a task in this task group. The signature matches that of [`asyncio.create_task()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task "asyncio.create_task"). If the task group is inactive (e.g. not yet entered, already finished, or in the process of shutting down), we will close the given `coro` and raise [`RuntimeError`](https://docs.python.org/3/builtins/exceptions.html#RuntimeError "RuntimeError").

Changed in version 3.13: Close the given coroutine if the task group is not active.

Changed in version 3.14: Passes on all _kwargs_ to [`loop.create_task()`](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.create_task "asyncio.loop.create_task")

#### cancel()

Cancel the task group. This is a non-exceptional, early exit of the task group’s lifetime – useful once the group’s goal has been met or its services no longer needed.

[`cancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancel "asyncio.Task.cancel") will be called on any tasks in the group that aren’t yet done, as well as the parent (body) of the group. The task group context manager will exit _without_ [`asyncio.CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") being raised.

If `cancel()` is called before entering the task group, the group will be cancelled upon entry. This is useful for patterns where one piece of code passes an unused [`asyncio.TaskGroup`](https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup "asyncio.TaskGroup") instance to another in order to have the ability to cancel anything run within the group.

`cancel()` is idempotent and may be called after the task group has already exited.

Some ways to use `cancel()`:

* call it from the task group body based on some condition or event
* pass the task group instance to child tasks via [`create_task()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task "asyncio.create_task"), allowing a child task to conditionally cancel the entire group
* pass the task group instance or bound `cancel()` method to some other task _before_ opening the task group, allowing remote cancellation

Added in version 3.15.

Example:

```
async def main():
    async with asyncio.TaskGroup() as tg:
        task1 = tg.create_task(some_coro(...))
        task2 = tg.create_task(another_coro(...))
    print(f"Both tasks have completed now: {task1.result()}, {task2.result()}")
```

The `async with` statement will wait for all tasks in the group to finish. While waiting, new tasks may still be added to the group (for example, by passing `tg` into one of the coroutines and calling `tg.create_task()` in that coroutine). There is also opportunity to request termination of the entire task group with `tg.cancel()`, based on some condition. Once the last task has finished and the `async with` block is exited, no new tasks may be added to the group.

The first time any of the tasks belonging to the group fails with an exception other than [`asyncio.CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError"), the remaining tasks in the group are cancelled. No further tasks can then be added to the group. At this point, if the body of the `async with` statement is still active (i.e., [`__aexit__()`](https://docs.python.org/3/reference/datamodel.html#object.__aexit__ "object.__aexit__") hasn’t been called yet), the task directly containing the `async with` statement is also cancelled. The resulting `asyncio.CancelledError` will interrupt an `await`, but it will not bubble out of the containing `async with` statement.

Once all tasks have finished, if any tasks have failed with an exception other than [`asyncio.CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError"), those exceptions are combined in an [`ExceptionGroup`](https://docs.python.org/3/builtins/exceptions.html#ExceptionGroup "ExceptionGroup") or [`BaseExceptionGroup`](https://docs.python.org/3/builtins/exceptions.html#BaseExceptionGroup "BaseExceptionGroup") (as appropriate; see their documentation) which is then raised.

Two base exceptions are treated specially: If any task fails with [`KeyboardInterrupt`](https://docs.python.org/3/builtins/exceptions.html#KeyboardInterrupt "KeyboardInterrupt") or [`SystemExit`](https://docs.python.org/3/builtins/exceptions.html#SystemExit "SystemExit"), the task group still cancels the remaining tasks and waits for them, but then the initial `KeyboardInterrupt` or `SystemExit` is re-raised instead of [`ExceptionGroup`](https://docs.python.org/3/builtins/exceptions.html#ExceptionGroup "ExceptionGroup") or [`BaseExceptionGroup`](https://docs.python.org/3/builtins/exceptions.html#BaseExceptionGroup "BaseExceptionGroup").

If the body of the `async with` statement exits with an exception (so [`__aexit__()`](https://docs.python.org/3/reference/datamodel.html#object.__aexit__ "object.__aexit__") is called with an exception set), this is treated the same as if one of the tasks failed: the remaining tasks are cancelled and then waited for, and non-cancellation exceptions are grouped into an exception group and raised. The exception passed into `__aexit__()`, unless it is [`asyncio.CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError"), is also included in the exception group. The same special case is made for [`KeyboardInterrupt`](https://docs.python.org/3/builtins/exceptions.html#KeyboardInterrupt "KeyboardInterrupt") and [`SystemExit`](https://docs.python.org/3/builtins/exceptions.html#SystemExit "SystemExit") as in the previous paragraph. There is an additional special case made only for the body of the `async with`: if it raises [`GeneratorExit`](https://docs.python.org/3/builtins/exceptions.html#GeneratorExit "GeneratorExit") and none of the other tasks raise exceptions that would be reported, then the `GeneratorExit` is reraised.

Task groups are careful not to mix up the internal cancellation used to “wake up” their [`__aexit__()`](https://docs.python.org/3/reference/datamodel.html#object.__aexit__ "object.__aexit__") with cancellation requests for the task in which they are running made by other parties. In particular, when one task group is syntactically nested in another, and both experience an exception in one of their child tasks simultaneously, the inner task group will process its exceptions, and then the outer task group will receive another cancellation and process its own exceptions.

In the case where a task group is cancelled externally and also must raise an [`ExceptionGroup`](https://docs.python.org/3/builtins/exceptions.html#ExceptionGroup "ExceptionGroup"), it will call the parent task’s [`cancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancel "asyncio.Task.cancel") method. This ensures that a [`asyncio.CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") will be raised at the next [`await`](https://docs.python.org/3/reference/expressions.html#await), so the cancellation is not lost.

Task groups preserve the cancellation count reported by [`asyncio.Task.cancelling()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancelling "asyncio.Task.cancelling").

Changed in version 3.13: Improved handling of simultaneous internal and external cancellations and correct preservation of cancellation counts.

Changed in version 3.15: Addition of the special case for [`GeneratorExit`](https://docs.python.org/3/builtins/exceptions.html#GeneratorExit "GeneratorExit").

## [Sleeping](https://docs.python.org/3/library/asyncio-task.html#id7)

### _async_ asyncio.sleep(_delay_, _result=None_)

Block for _delay_ seconds.

If _result_ is provided, it is returned to the caller when the coroutine completes.

`sleep()` always suspends the current task, allowing other tasks to run.

Setting the delay to 0 provides an optimized path to allow other tasks to run. This can be used by long-running functions to avoid blocking the event loop for the full duration of the function call.

Example of coroutine displaying the current date every second for 5 seconds:

```
import asyncio
import datetime as dt

async def display_date():
    loop = asyncio.get_running_loop()
    end_time = loop.time() + 5.0
    while True:
        print(dt.datetime.now())
        if (loop.time() + 1.0) >= end_time:
            break
        await asyncio.sleep(1)

asyncio.run(display_date())
```

Changed in version 3.10: Removed the _loop_ parameter.

Changed in version 3.13: Raises [`ValueError`](https://docs.python.org/3/builtins/exceptions.html#ValueError "ValueError") if _delay_ is [`nan`](https://docs.python.org/3/library/math.html#math.nan "math.nan").

## [Running tasks concurrently](https://docs.python.org/3/library/asyncio-task.html#id8)

### _awaitable_ asyncio.gather(_*aws_, _return_exceptions=False_)

Run [awaitable objects](https://docs.python.org/3/library/asyncio-task.html#asyncio-awaitables) in the _aws_ sequence _concurrently_.

If any awaitable in _aws_ is a coroutine, it is automatically scheduled as a Task.

If all awaitables are completed successfully, the result is an aggregate list of returned values. The order of result values corresponds to the order of awaitables in _aws_.

If _return_exceptions_ is `False` (default), the first raised exception is immediately propagated to the task that awaits on `gather()`. Other awaitables in the _aws_ sequence __won’t be cancelled__ and will continue to run.

If _return_exceptions_ is `True`, exceptions are treated the same as successful results, and aggregated in the result list.

If `gather()` is _cancelled_, all submitted awaitables (that have not completed yet) are also _cancelled_.

If any Task or Future from the _aws_ sequence is _cancelled_, it is treated as if it raised [`CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") – the `gather()` call is __not__ cancelled in this case. This is to prevent the cancellation of one submitted Task/Future to cause other Tasks/Futures to be cancelled.

Note

A new alternative to create and run tasks concurrently and wait for their completion is [`asyncio.TaskGroup`](https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup "asyncio.TaskGroup"). _TaskGroup_ provides stronger safety guarantees than _gather_ for scheduling a nesting of subtasks: if a task (or a subtask, a task scheduled by a task) raises an exception, _TaskGroup_ will, while _gather_ will not, cancel the remaining scheduled tasks.

Example:

```
import asyncio

async def factorial(name, number):
    f = 1
    for i in range(2, number + 1):
        print(f"Task {name}: Compute factorial({number}), currently i={i}...")
        await asyncio.sleep(1)
        f *= i
    print(f"Task {name}: factorial({number}) = {f}")
    return f

async def main():
    # Schedule three calls *concurrently*:
    L = await asyncio.gather(
        factorial("A", 2),
        factorial("B", 3),
        factorial("C", 4),
    )
    print(L)

asyncio.run(main())

# Expected output:
#
#     Task A: Compute factorial(2), currently i=2...
#     Task B: Compute factorial(3), currently i=2...
#     Task C: Compute factorial(4), currently i=2...
#     Task A: factorial(2) = 2
#     Task B: Compute factorial(3), currently i=3...
#     Task C: Compute factorial(4), currently i=3...
#     Task B: factorial(3) = 6
#     Task C: Compute factorial(4), currently i=4...
#     Task C: factorial(4) = 24
#     [2, 6, 24]
```

Note

If _return_exceptions_ is false, cancelling gather() after it has been marked done won’t cancel any submitted awaitables. For instance, gather can be marked done after propagating an exception to the caller, therefore, calling `gather.cancel()` after catching an exception (raised by one of the awaitables) from gather won’t cancel any other awaitables.

Changed in version 3.7: If the _gather_ itself is cancelled, the cancellation is propagated regardless of _return_exceptions_.

Changed in version 3.10: Removed the _loop_ parameter.

Deprecated since version 3.10: Deprecation warning is emitted if no positional arguments are provided or not all positional arguments are Future-like objects and there is no running event loop.

## [Eager task factory](https://docs.python.org/3/library/asyncio-task.html#id9)

### asyncio.eager_task_factory(_loop_, _coro_, _*_, _name=None_, _context=None_)

A task factory for eager task execution.

When using this factory (via [`loop.set_task_factory(asyncio.eager_task_factory)`](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.set_task_factory "asyncio.loop.set_task_factory")), coroutines begin execution synchronously during [`Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task") construction. Tasks are only scheduled on the event loop if they block. This can be a performance improvement as the overhead of loop scheduling is avoided for coroutines that complete synchronously.

A common example where this is beneficial is coroutines which employ caching or memoization to avoid actual I/O when possible.

Note

Immediate execution of the coroutine is a semantic change. If the coroutine returns or raises, the task is never scheduled to the event loop. If the coroutine execution blocks, the task is scheduled to the event loop. This change may introduce behavior changes to existing applications. For example, the application’s task execution order is likely to change.

Added in version 3.12.

### asyncio.create_eager_task_factory(_custom_task_constructor_)

Create an eager task factory, similar to [`eager_task_factory()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.eager_task_factory "asyncio.eager_task_factory"), using the provided _custom_task_constructor_ when creating a new task instead of the default [`Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task").

_custom_task_constructor_ must be a _callable_ with the signature matching the signature of [`Task.__init__`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task"). The callable must return a [`asyncio.Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task")-compatible object.

This function returns a _callable_ intended to be used as a task factory of an event loop via [`loop.set_task_factory(factory)`](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.set_task_factory "asyncio.loop.set_task_factory")).

Added in version 3.12.

## [Shielding from cancellation](https://docs.python.org/3/library/asyncio-task.html#id10)

### _awaitable_ asyncio.shield(_aw_)

Protect an [awaitable object](https://docs.python.org/3/library/asyncio-task.html#asyncio-awaitables) from being [`cancelled`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancel "asyncio.Task.cancel").

If _aw_ is a coroutine it is automatically scheduled as a Task.

The statement:

```
task = asyncio.create_task(something())
res = await shield(task)
```

is equivalent to:

```
res = await something()
```

_except_ that if the coroutine containing it is cancelled, the Task running in `something()` is not cancelled. From the point of view of `something()`, the cancellation did not happen. Although its caller is still cancelled, so the “await” expression still raises a [`CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError").

If `something()` is cancelled by other means (i.e. from within itself) that would also cancel `shield()`.

If it is desired to completely ignore cancellation (not recommended) the `shield()` function should be combined with a try/except clause, as follows:

```
task = asyncio.create_task(something())
try:
    res = await shield(task)
except CancelledError:
    res = None
```

Important

Save a reference to tasks passed to this function, to avoid a task disappearing mid-execution. The event loop only keeps weak references to tasks. A task that isn’t referenced elsewhere may get garbage collected at any time, even before it’s done.

Changed in version 3.10: Removed the _loop_ parameter.

Deprecated since version 3.10: Deprecation warning is emitted if _aw_ is not Future-like object and there is no running event loop.

## [Timeouts](https://docs.python.org/3/library/asyncio-task.html#id11)

### asyncio.timeout(_delay_)

Return an [asynchronous context manager](https://docs.python.org/3/reference/datamodel.html#async-context-managers) that can be used to limit the amount of time spent waiting on something.

_delay_ can either be `None`, or a float/int number of seconds to wait. If _delay_ is `None`, no time limit will be applied; this can be useful if the delay is unknown when the context manager is created.

In either case, the context manager can be rescheduled after creation using [`Timeout.reschedule()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Timeout.reschedule "asyncio.Timeout.reschedule").

Example:

```
async def main():
    async with asyncio.timeout(10):
        await long_running_task()
```

If `long_running_task` takes more than 10 seconds to complete, the context manager will cancel the current task and handle the resulting [`asyncio.CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") internally, transforming it into a [`TimeoutError`](https://docs.python.org/3/builtins/exceptions.html#TimeoutError "TimeoutError") which can be caught and handled.

Note

The [`asyncio.timeout()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout "asyncio.timeout") context manager is what transforms the [`asyncio.CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") into a [`TimeoutError`](https://docs.python.org/3/builtins/exceptions.html#TimeoutError "TimeoutError"), which means the `TimeoutError` can only be caught _outside_ of the context manager.

Example of catching [`TimeoutError`](https://docs.python.org/3/builtins/exceptions.html#TimeoutError "TimeoutError"):

```
async def main():
    try:
        async with asyncio.timeout(10):
            await long_running_task()
    except TimeoutError:
        print("The long operation timed out, but we've handled it.")

    print("This statement will run regardless.")
```

The context manager produced by [`asyncio.timeout()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout "asyncio.timeout") can be rescheduled to a different deadline and inspected.

#### _class_ asyncio.Timeout(_when_)

An [asynchronous context manager](https://docs.python.org/3/reference/datamodel.html#async-context-managers) for cancelling overdue coroutines.

Prefer using [`asyncio.timeout()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout "asyncio.timeout") or [`asyncio.timeout_at()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout_at "asyncio.timeout_at") rather than instantiating `Timeout` directly.

`when` should be an absolute time at which the context should time out, as measured by the event loop’s clock:

* If `when` is `None`, the timeout will never trigger.
* If `when < loop.time()`, the timeout will trigger on the next iteration of the event loop.

> ##### when() → [float](https://docs.python.org/3/builtins/functions.html#float "float") | [None](https://docs.python.org/3/builtins/constants.html#None "None")[¶](https://docs.python.org/3/library/asyncio-task.html#asyncio.Timeout.when "Link to this definition")
>
> Return the current deadline, or `None` if the current deadline is not set.
>
> ##### reschedule(_when: [float](https://docs.python.org/3/builtins/functions.html#float "float") | [None](https://docs.python.org/3/builtins/constants.html#None "None")_)[¶](https://docs.python.org/3/library/asyncio-task.html#asyncio.Timeout.reschedule "Link to this definition")
>
> Reschedule the timeout.
>
> ##### expired() → [bool](https://docs.python.org/3/builtins/functions.html#bool "bool")[¶](https://docs.python.org/3/library/asyncio-task.html#asyncio.Timeout.expired "Link to this definition")
>
> Return whether the context manager has exceeded its deadline (expired).

Example:

```
async def main():
    try:
        # We do not know the timeout when starting, so we pass ``None``.
        async with asyncio.timeout(None) as cm:
            # We know the timeout now, so we reschedule it.
            new_deadline = get_running_loop().time() + 10
            cm.reschedule(new_deadline)

            await long_running_task()
    except TimeoutError:
        pass

    if cm.expired():
        print("Looks like we haven't finished on time.")
```

Timeout context managers can be safely nested.

Added in version 3.11.

### asyncio.timeout_at(_when_)

Similar to [`asyncio.timeout()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout "asyncio.timeout"), except _when_ is the absolute time to stop waiting, or `None`.

Example:

```
async def main():
    loop = get_running_loop()
    deadline = loop.time() + 20
    try:
        async with asyncio.timeout_at(deadline):
            await long_running_task()
    except TimeoutError:
        print("The long operation timed out, but we've handled it.")

    print("This statement will run regardless.")
```

Added in version 3.11.

### _async_ asyncio.wait_for(_aw_, _timeout_)

Wait for the _aw_ [awaitable](https://docs.python.org/3/library/asyncio-task.html#asyncio-awaitables) to complete with a timeout.

_timeout_ can either be `None` or a float or int number of seconds to wait for. If _timeout_ is `None`, block until the future completes.

If a timeout occurs, it cancels _aw_ and raises [`TimeoutError`](https://docs.python.org/3/builtins/exceptions.html#TimeoutError "TimeoutError").

To prevent _aw_ from being cancelled, wrap it in [`shield()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.shield "asyncio.shield").

The function will wait until the future is actually cancelled, so the total wait time may exceed the _timeout_. If an exception happens during cancellation, it is propagated.

If the wait is cancelled, the future _aw_ is also cancelled.

Example:

```
async def eternity():
    # Sleep for one hour
    await asyncio.sleep(3600)
    print('yay!')

async def main():
    # Wait for at most 1 second
    try:
        await asyncio.wait_for(eternity(), timeout=1.0)
    except TimeoutError:
        print('timeout!')

asyncio.run(main())

# Expected output:
#
#     timeout!
```

Changed in version 3.7: When _aw_ is cancelled due to a timeout, `wait_for` waits for _aw_ to be cancelled. Previously, it raised [`TimeoutError`](https://docs.python.org/3/builtins/exceptions.html#TimeoutError "TimeoutError") immediately.

Changed in version 3.10: Removed the _loop_ parameter.

Changed in version 3.11: Raises [`TimeoutError`](https://docs.python.org/3/builtins/exceptions.html#TimeoutError "TimeoutError") instead of [`asyncio.TimeoutError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.TimeoutError "asyncio.TimeoutError").

Changed in version 3.12: Implemented using [`asyncio.timeout()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout "asyncio.timeout"), a coroutine passed as _aw_ is no longer wrapped in a [`Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task") when _timeout_ is positive.

## [Waiting primitives](https://docs.python.org/3/library/asyncio-task.html#id12)

### _async_ asyncio.wait(_aws_, _*_, _timeout=None_, _return_when=ALL_COMPLETED_)

Run [`Future`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future "asyncio.Future") and [`Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task") instances in the _aws_ iterable concurrently and block until the condition specified by _return_when_.

The _aws_ iterable must not be empty.

Returns two sets of Tasks/Futures: `(done, pending)`.

Usage:

```
done, pending = await asyncio.wait(aws)
```

_timeout_ (a float or int), if specified, can be used to control the maximum number of seconds to wait before returning.

Note that this function does not raise [`TimeoutError`](https://docs.python.org/3/builtins/exceptions.html#TimeoutError "TimeoutError"). Futures or Tasks that aren’t done when the timeout occurs are simply returned in the second set.

_return_when_ indicates when this function should return. It must be one of the following constants:

| Constant | Description |
| --- | --- |
| asyncio.FIRST_COMPLETED[¶](https://docs.python.org/3/library/asyncio-task.html#asyncio.FIRST_COMPLETED "Link to this definition") | The function will return when any future finishes or is cancelled. |
| asyncio.FIRST_EXCEPTION[¶](https://docs.python.org/3/library/asyncio-task.html#asyncio.FIRST_EXCEPTION "Link to this definition") | The function will return when any future finishes by raising an exception. If no future raises an exception then it is equivalent to [`ALL_COMPLETED`](https://docs.python.org/3/library/asyncio-task.html#asyncio.ALL_COMPLETED "asyncio.ALL_COMPLETED"). |
| asyncio.ALL_COMPLETED[¶](https://docs.python.org/3/library/asyncio-task.html#asyncio.ALL_COMPLETED "Link to this definition") | The function will return when all futures finish or are cancelled. |

Unlike [`wait_for()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for "asyncio.wait_for"), `wait()` does not cancel the futures when a timeout occurs.

If `wait()` is cancelled, the futures in _aws_ are not cancelled and continue to run.

Changed in version 3.10: Removed the _loop_ parameter.

Changed in version 3.11: Passing coroutine objects to `wait()` directly is forbidden.

Changed in version 3.12: Added support for generators yielding tasks.

### asyncio.as_completed(_aws_, _*_, _timeout=None_)

Run [awaitable objects](https://docs.python.org/3/library/asyncio-task.html#asyncio-awaitables) in the _aws_ iterable concurrently. The returned object can be iterated to obtain the results of the awaitables as they finish.

The object returned by `as_completed()` can be iterated as an [asynchronous iterator](https://docs.python.org/3/glossary.html#term-asynchronous-iterator) or a plain [iterator](https://docs.python.org/3/glossary.html#term-iterator). When asynchronous iteration is used, the originally-supplied awaitables are yielded if they are tasks or futures. This makes it easy to correlate previously-scheduled tasks with their results. Example:

```
ipv4_connect = create_task(open_connection("127.0.0.1", 80))
ipv6_connect = create_task(open_connection("::1", 80))
tasks = [ipv4_connect, ipv6_connect]

async for earliest_connect in as_completed(tasks):
    # earliest_connect is done. The result can be obtained by
    # awaiting it or calling earliest_connect.result()
    reader, writer = await earliest_connect

    if earliest_connect is ipv6_connect:
        print("IPv6 connection established.")
    else:
        print("IPv4 connection established.")
```

During asynchronous iteration, implicitly-created tasks will be yielded for supplied awaitables that aren’t tasks or futures.

When used as a plain iterator, each iteration yields a new coroutine that returns the result or raises the exception of the next completed awaitable. This pattern is compatible with Python versions older than 3.13:

```
ipv4_connect = create_task(open_connection("127.0.0.1", 80))
ipv6_connect = create_task(open_connection("::1", 80))
tasks = [ipv4_connect, ipv6_connect]

for next_connect in as_completed(tasks):
    # next_connect is not one of the original task objects. It must be
    # awaited to obtain the result value or raise the exception of the
    # awaitable that finishes next.
    reader, writer = await next_connect
```

A [`TimeoutError`](https://docs.python.org/3/builtins/exceptions.html#TimeoutError "TimeoutError") is raised if the timeout occurs before all awaitables are done. This is raised by the `async for` loop during asynchronous iteration or by the coroutines yielded during plain iteration.

`as_completed()` does not cancel the tasks running the supplied awaitables: if a timeout occurs or the iteration is cancelled, the remaining tasks continue to run.

Changed in version 3.10: Removed the _loop_ parameter.

Deprecated since version 3.10: Deprecation warning is emitted if not all awaitable objects in the _aws_ iterable are Future-like objects and there is no running event loop.

Changed in version 3.12: Added support for generators yielding tasks.

Changed in version 3.13: The result can now be used as either an [asynchronous iterator](https://docs.python.org/3/glossary.html#term-asynchronous-iterator) or as a plain [iterator](https://docs.python.org/3/glossary.html#term-iterator) (previously it was only a plain iterator).

## [Running in threads](https://docs.python.org/3/library/asyncio-task.html#id13)

### _async_ asyncio.to_thread(_func_, _/_, _*args_, _**kwargs_)

Asynchronously run function _func_ in a separate thread.

Any *args and **kwargs supplied for this function are directly passed to _func_. Also, the current [`contextvars.Context`](https://docs.python.org/3/library/contextvars.html#contextvars.Context "contextvars.Context") is propagated, allowing context variables from the event loop thread to be accessed in the separate thread.

Return a coroutine that can be awaited to get the eventual result of _func_.

This coroutine function is primarily intended to be used for executing IO-bound functions/methods that would otherwise block the event loop if they were run in the main thread. For example:

```
def blocking_io():
    print(f"start blocking_io at {time.strftime('%X')}")
    # Note that time.sleep() can be replaced with any blocking
    # IO-bound operation, such as file operations.
    time.sleep(1)
    print(f"blocking_io complete at {time.strftime('%X')}")

async def main():
    print(f"started main at {time.strftime('%X')}")

    await asyncio.gather(
        asyncio.to_thread(blocking_io),
        asyncio.sleep(1))

    print(f"finished main at {time.strftime('%X')}")


asyncio.run(main())

# Expected output:
#
# started main at 19:50:53
# start blocking_io at 19:50:53
# blocking_io complete at 19:50:54
# finished main at 19:50:54
```

Directly calling `blocking_io()` in any coroutine would block the event loop for its duration, resulting in an additional 1 second of run time. Instead, by using `asyncio.to_thread()`, we can run it in a separate thread without blocking the event loop.

Note

Due to the [GIL](https://docs.python.org/3/glossary.html#term-GIL), `asyncio.to_thread()` can typically only be used to make IO-bound functions non-blocking. However, for extension modules that release the GIL or alternative Python implementations that don’t have one, `asyncio.to_thread()` can also be used for CPU-bound functions.

Added in version 3.9.

## [Scheduling from other threads](https://docs.python.org/3/library/asyncio-task.html#id14)

### asyncio.run_coroutine_threadsafe(_coro_, _loop_)

Submit a coroutine to the given event loop. Thread-safe.

Return a [`concurrent.futures.Future`](https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Future "concurrent.futures.Future") to wait for the result from another OS thread.

This function is meant to be called from a different OS thread than the one where the event loop is running. Example:

```
def in_thread(loop: asyncio.AbstractEventLoop) -> None:
    # Run some blocking IO
    pathlib.Path("example.txt").write_text("hello world", encoding="utf8")

    # Create a coroutine
    coro = asyncio.sleep(1, result=3)

    # Submit the coroutine to a given loop
    future = asyncio.run_coroutine_threadsafe(coro, loop)

    # Wait for the result with an optional timeout argument
    assert future.result(timeout=2) == 3

async def amain() -> None:
    # Get the running loop
    loop = asyncio.get_running_loop()

    # Run something in a thread
    await asyncio.to_thread(in_thread, loop)
```

It’s also possible to run the other way around. Example:

```
@contextlib.contextmanager
def loop_in_thread() -> Generator[asyncio.AbstractEventLoop]:
    loop_fut = concurrent.futures.Future[asyncio.AbstractEventLoop]()
    stop_event = asyncio.Event()

    async def main() -> None:
        loop_fut.set_result(asyncio.get_running_loop())
        await stop_event.wait()

    with concurrent.futures.ThreadPoolExecutor(1) as tpe:
        complete_fut = tpe.submit(asyncio.run, main())
        for fut in concurrent.futures.as_completed((loop_fut, complete_fut)):
            if fut is loop_fut:
                loop = loop_fut.result()
                try:
                    yield loop
                finally:
                    loop.call_soon_threadsafe(stop_event.set)
            else:
                fut.result()

# Create a loop in another thread
with loop_in_thread() as loop:
    # Create a coroutine
    coro = asyncio.sleep(1, result=3)

    # Submit the coroutine to a given loop
    future = asyncio.run_coroutine_threadsafe(coro, loop)

    # Wait for the result with an optional timeout argument
    assert future.result(timeout=2) == 3
```

If an exception is raised in the coroutine, the returned Future will be notified. It can also be used to cancel the task in the event loop:

```
try:
    result = future.result(timeout)
except TimeoutError:
    print('The coroutine took too long, cancelling the task...')
    future.cancel()
except Exception as exc:
    print(f'The coroutine raised an exception: {exc!r}')
else:
    print(f'The coroutine returned: {result!r}')
```

See the [concurrency and multithreading](https://docs.python.org/3/library/asyncio-dev.html#asyncio-multithreading) section of the documentation.

Unlike other asyncio functions this function requires the _loop_ argument to be passed explicitly.

Added in version 3.5.1.

## [Introspection](https://docs.python.org/3/library/asyncio-task.html#id15)

### asyncio.current_task(_loop=None_)

Return the currently running [`Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task") instance, or `None` if no task is running.

If _loop_ is `None` [`get_running_loop()`](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.get_running_loop "asyncio.get_running_loop") is used to get the current loop.

Added in version 3.7.

### asyncio.all_tasks(_loop=None_)

Return a set of not yet finished [`Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task") objects run by the loop.

If _loop_ is `None`, [`get_running_loop()`](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.get_running_loop "asyncio.get_running_loop") is used for getting current loop.

Added in version 3.7.

### asyncio.iscoroutine(_obj_)

Return `True` if _obj_ is a coroutine object.

Added in version 3.4.

## [Task object](https://docs.python.org/3/library/asyncio-task.html#id16)

### _class_ asyncio.Task(_coro_, _*_, _loop=None_, _name=None_, _context=None_, _eager_start=False_)

A [`Future-like`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future "asyncio.Future") object that runs a Python [coroutine](https://docs.python.org/3/library/asyncio-task.html#coroutine). Not thread-safe.

Tasks are used to run coroutines in event loops. If a coroutine awaits on a Future, the Task suspends the execution of the coroutine and waits for the completion of the Future. When the Future is _done_, the execution of the wrapped coroutine resumes.

Event loops use cooperative scheduling: an event loop runs one Task at a time. While a Task awaits for the completion of a Future, the event loop runs other Tasks, callbacks, or performs IO operations.

Use the high-level [`asyncio.create_task()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task "asyncio.create_task") function to create Tasks, or the low-level [`loop.create_task()`](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.create_task "asyncio.loop.create_task") or [`ensure_future()`](https://docs.python.org/3/library/asyncio-future.html#asyncio.ensure_future "asyncio.ensure_future") functions. Manual instantiation of Tasks is discouraged.

To cancel a running Task use the [`cancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancel "asyncio.Task.cancel") method. Calling it will cause the Task to throw a [`CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") exception into the wrapped coroutine. If a coroutine is awaiting on a future-like object during cancellation, the awaited object will be cancelled.

[`cancelled()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancelled "asyncio.Task.cancelled") can be used to check if the Task was cancelled. The method returns `True` if the wrapped coroutine did not suppress the [`CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") exception and was actually cancelled.

[`asyncio.Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task") inherits from [`Future`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future "asyncio.Future") all of its APIs except [`Future.set_result()`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future.set_result "asyncio.Future.set_result") and [`Future.set_exception()`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future.set_exception "asyncio.Future.set_exception").

An optional keyword-only _context_ argument allows specifying a custom [`contextvars.Context`](https://docs.python.org/3/library/contextvars.html#contextvars.Context "contextvars.Context") for the _coro_ to run in. If no _context_ is provided, the Task copies the current context and later runs its coroutine in the copied context.

An optional keyword-only _eager_start_ argument allows eagerly starting the execution of the [`asyncio.Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task "asyncio.Task") at task creation time. If set to `True` and the event loop is running, the task will start executing the coroutine immediately, until the first time the coroutine blocks. If the coroutine returns or raises without blocking, the task will be finished eagerly and will skip scheduling to the event loop.

Tasks are [generic](https://docs.python.org/3/library/typing.html#generics) over the return type of their wrapped coroutines.

Changed in version 3.7: Added support for the [`contextvars`](https://docs.python.org/3/library/contextvars.html#module-contextvars "contextvars: Context Variables") module.

Changed in version 3.8: Added the _name_ parameter.

Deprecated since version 3.10: Deprecation warning is emitted if _loop_ is not specified and there is no running event loop.

Changed in version 3.11: Added the _context_ parameter.

Changed in version 3.12: Added the _eager_start_ parameter.

#### done()

Return `True` if the Task is _done_.

A Task is _done_ when the wrapped coroutine either returned a value, raised an exception, or the Task was cancelled.

#### result()

Return the result of the Task.

If the Task is _done_, the result of the wrapped coroutine is returned (or if the coroutine raised an exception, that exception is re-raised.)

If the Task has been _cancelled_, this method raises a [`CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") exception.

If the Task’s result isn’t yet available, this method raises an [`InvalidStateError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.InvalidStateError "asyncio.InvalidStateError") exception.

#### exception()

Return the exception of the Task.

If the wrapped coroutine raised an exception that exception is returned. If the wrapped coroutine returned normally this method returns `None`.

If the Task has been _cancelled_, this method raises a [`CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") exception.

If the Task isn’t _done_ yet, this method raises an [`InvalidStateError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.InvalidStateError "asyncio.InvalidStateError") exception.

#### add_done_callback(_callback_, _*_, _context=None_)

Add a callback to be run when the Task is _done_.

This method should only be used in low-level callback-based code.

See the documentation of [`Future.add_done_callback()`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future.add_done_callback "asyncio.Future.add_done_callback") for more details.

#### remove_done_callback(_callback_)

Remove _callback_ from the callbacks list.

This method should only be used in low-level callback-based code.

See the documentation of [`Future.remove_done_callback()`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future.remove_done_callback "asyncio.Future.remove_done_callback") for more details.

#### get_stack(_*_, _limit=None_)

Return the list of stack frames for this Task.

If the wrapped coroutine is not done, this returns the stack where it is suspended. If the coroutine has completed successfully or was cancelled, this returns an empty list. If the coroutine was terminated by an exception, this returns the list of traceback frames.

The frames are always ordered from oldest to newest.

Only one stack frame is returned for a suspended coroutine.

The optional _limit_ argument sets the maximum number of frames to return; by default all available frames are returned. The ordering of the returned list differs depending on whether a stack or a traceback is returned: the newest frames of a stack are returned, but the oldest frames of a traceback are returned. (This matches the behavior of the traceback module.)

#### print_stack(_*_, _limit=None_, _file=None_)

Print the stack or traceback for this Task.

This produces output similar to that of the traceback module for the frames retrieved by [`get_stack()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.get_stack "asyncio.Task.get_stack").

The _limit_ argument is passed to [`get_stack()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.get_stack "asyncio.Task.get_stack") directly.

The _file_ argument is an I/O stream to which the output is written; by default output is written to [`sys.stdout`](https://docs.python.org/3/library/sys.html#sys.stdout "sys.stdout").

#### get_coro()

Return the coroutine object wrapped by the `Task`.

Note

This will return `None` for Tasks which have already completed eagerly. See the [Eager Task Factory](https://docs.python.org/3/library/asyncio-task.html#eager-task-factory).

Added in version 3.8.

Changed in version 3.12: Newly added eager task execution means result may be `None`.

#### get_context()

Return the [`contextvars.Context`](https://docs.python.org/3/library/contextvars.html#contextvars.Context "contextvars.Context") object associated with the task.

Added in version 3.12.

#### get_name()

Return the name of the Task.

If no name has been explicitly assigned to the Task, the default asyncio Task implementation generates a default name during instantiation.

Added in version 3.8.

#### set_name(_value_)

Set the name of the Task.

The _value_ argument can be any object, which is then converted to a string.

In the default Task implementation, the name will be visible in the [`repr()`](https://docs.python.org/3/builtins/functions.html#repr "repr") output of a task object.

Added in version 3.8.

#### cancel(_msg=None_)

Request the Task to be cancelled.

If the Task is already _done_ or _cancelled_, return `False`, otherwise, return `True`.

The method arranges for a [`CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") exception to be thrown into the wrapped coroutine on the next cycle of the event loop.

The coroutine then has a chance to clean up or even deny the request by suppressing the exception with a [`try`](https://docs.python.org/3/reference/compound_stmts.html#try) … … `except CancelledError` … [`finally`](https://docs.python.org/3/reference/compound_stmts.html#finally) block. Therefore, unlike [`Future.cancel()`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future.cancel "asyncio.Future.cancel"), `Task.cancel()` does not guarantee that the Task will be cancelled, although suppressing cancellation completely is not common and is actively discouraged. Should the coroutine nevertheless decide to suppress the cancellation, it needs to call [`Task.uncancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.uncancel "asyncio.Task.uncancel") in addition to catching the exception.

If the Task being cancelled is currently awaiting on a future-like object, that awaited object will also be cancelled. This cancellation propagates down the entire chain of awaited objects.

Changed in version 3.9: Added the _msg_ parameter.

Changed in version 3.11: The `msg` parameter is propagated from cancelled task to its awaiter.

The following example illustrates how coroutines can intercept the cancellation request:

```
async def cancel_me():
    print('cancel_me(): before sleep')

    try:
        # Wait for 1 hour
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        print('cancel_me(): cancel sleep')
        raise
    finally:
        print('cancel_me(): after sleep')

async def main():
    # Create a "cancel_me" Task
    task = asyncio.create_task(cancel_me())

    # Wait for 1 second
    await asyncio.sleep(1)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        print("main(): cancel_me is cancelled now")

asyncio.run(main())

# Expected output:
#
#     cancel_me(): before sleep
#     cancel_me(): cancel sleep
#     cancel_me(): after sleep
#     main(): cancel_me is cancelled now
```

#### cancelled()

Return `True` if the Task is _cancelled_.

The Task is _cancelled_ when the cancellation was requested with [`cancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancel "asyncio.Task.cancel") and the wrapped coroutine propagated the [`CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") exception thrown into it.

#### uncancel()

Decrement the count of cancellation requests to this Task.

Returns the remaining number of cancellation requests.

Note that once execution of a cancelled task completed, further calls to `uncancel()` are ineffective.

Added in version 3.11.

This method is used by asyncio’s internals and isn’t expected to be used by end-user code. In particular, if a Task gets successfully uncancelled, this allows for elements of structured concurrency like [Task groups](https://docs.python.org/3/library/asyncio-task.html#taskgroups) and [`asyncio.timeout()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout "asyncio.timeout") to continue running, isolating cancellation to the respective structured block. For example:

```
async def make_request_with_timeout():
    try:
        async with asyncio.timeout(1):
            # Structured block affected by the timeout:
            await make_request()
            await make_another_request()
    except TimeoutError:
        log("There was a timeout")
    # Outer code not affected by the timeout:
    await unrelated_code()
```

While the block with `make_request()` and `make_another_request()` might get cancelled due to the timeout, `unrelated_code()` should continue running even in case of the timeout. This is implemented with `uncancel()`. [`TaskGroup`](https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup "asyncio.TaskGroup") context managers use [`uncancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.uncancel "asyncio.Task.uncancel") in a similar fashion.

If end-user code is, for some reason, suppressing cancellation by catching [`CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError"), it needs to call this method to remove the cancellation state.

When this method decrements the cancellation count to zero, the method checks if a previous [`cancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancel "asyncio.Task.cancel") call had arranged for [`CancelledError`](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError "asyncio.CancelledError") to be thrown into the task. If it hasn’t been thrown yet, that arrangement will be rescinded (by resetting the internal `_must_cancel` flag).

Changed in version 3.13: Changed to rescind pending cancellation requests upon reaching zero.

#### cancelling()

Return the number of pending cancellation requests to this Task, i.e., the number of calls to [`cancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancel "asyncio.Task.cancel") less the number of [`uncancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.uncancel "asyncio.Task.uncancel") calls.

Note that if this number is greater than zero but the Task is still executing, [`cancelled()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancelled "asyncio.Task.cancelled") will still return `False`. This is because this number can be lowered by calling [`uncancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.uncancel "asyncio.Task.uncancel"), which can lead to the task not being cancelled after all if the cancellation requests go down to zero.

This method is used by asyncio’s internals and isn’t expected to be used by end-user code. See [`uncancel()`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.uncancel "asyncio.Task.uncancel") for more details.

Added in version 3.11.

### [Table of Contents](https://docs.python.org/3/contents.html)

* [Coroutines and tasks](https://docs.python.org/3/library/asyncio-task.html)
  * [Coroutines](https://docs.python.org/3/library/asyncio-task.html#coroutines)
  * [Awaitables](https://docs.python.org/3/library/asyncio-task.html#awaitables)
  * [Creating tasks](https://docs.python.org/3/library/asyncio-task.html#creating-tasks)
  * [Task cancellation](https://docs.python.org/3/library/asyncio-task.html#task-cancellation)
  * [Task groups](https://docs.python.org/3/library/asyncio-task.html#task-groups)
  * [Sleeping](https://docs.python.org/3/library/asyncio-task.html#sleeping)
  * [Running tasks concurrently](https://docs.python.org/3/library/asyncio-task.html#running-tasks-concurrently)
  * [Eager task factory](https://docs.python.org/3/library/asyncio-task.html#eager-task-factory)
  * [Shielding from cancellation](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation)
  * [Timeouts](https://docs.python.org/3/library/asyncio-task.html#timeouts)
  * [Waiting primitives](https://docs.python.org/3/library/asyncio-task.html#waiting-primitives)
  * [Running in threads](https://docs.python.org/3/library/asyncio-task.html#running-in-threads)
  * [Scheduling from other threads](https://docs.python.org/3/library/asyncio-task.html#scheduling-from-other-threads)
  * [Introspection](https://docs.python.org/3/library/asyncio-task.html#introspection)
  * [Task object](https://docs.python.org/3/library/asyncio-task.html#task-object)

#### Previous topic

[Runners](https://docs.python.org/3/library/asyncio-runner.html "previous chapter")

#### Next topic

[Streams](https://docs.python.org/3/library/asyncio-stream.html "next chapter")

### This page

* [Report a bug](https://docs.python.org/3/bugs.html)
* [Improve this page](https://docs.python.org/3/improve-page.html?pagetitle=Coroutines+and+tasks&pageurl=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio-task.html&pagesource=library%2Fasyncio-task.rst)
* [Show source](https://github.com/python/cpython/blob/3.15/Doc/library/asyncio-task.rst?plain=1)

«

### Navigation

* [index](https://docs.python.org/3/genindex.html "General Index")
* [modules](https://docs.python.org/3/py-modindex.html "Python Module Index") |
* [next](https://docs.python.org/3/library/asyncio-stream.html "Streams") |
* [previous](https://docs.python.org/3/library/asyncio-runner.html "Runners") |
* ![Python logo](https://docs.python.org/3/_static/py.svg)
* [Python](https://www.python.org/) »

* [3.15.0 Documentation](https://docs.python.org/3/index.html) »
* [The Python standard library](https://docs.python.org/3/library/index.html) »
* [Networking and Interprocess Communication](https://docs.python.org/3/library/ipc.html) »
* [`asyncio` — Asynchronous I/O](https://docs.python.org/3/library/asyncio.html) »
* [Coroutines and tasks](https://docs.python.org/3/library/asyncio-task.html)
* |
* Theme  |

© [Copyright](https://docs.python.org/3/copyright.html) 2001 Python Software Foundation.   
 This page is licensed under the Python Software Foundation License Version 2.   
 Examples, recipes, and other code in the documentation are additionally licensed under the Zero Clause BSD License.   
 See [History and License](https://docs.python.org/license.html) for more information.  
   
 The Python Software Foundation is a non-profit corporation. [Please donate.](https://www.python.org/psf/donations/)   
   
 Last updated on Oct 14, 2026 (13:06 UTC). [Found a bug](https://docs.python.org/bugs.html)?   
 Created using [Sphinx](https://www.sphinx-doc.org/) 8.2.3.
