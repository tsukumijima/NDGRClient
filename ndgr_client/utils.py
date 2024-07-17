"""
ref: https://github.com/anomaly/gallagher/blob/master/gallagher/cli/utils.py

Asyncio patches for Typer

There's a thread open on the typer repo about this:
    https://github.com/tiangolo/typer/issues/88

Essentially, Typer doesn't support async functions. This is an issue
post migration to async, @csheppard points out that there's a work
around using a decorator on the click repository:
https://github.com/tiangolo/typer/issues/88#issuecomment-612687289

@gilcu2 posted a similar solution on the typer repo:
https://github.com/tiangolo/typer/issues/88#issuecomment-1732469681

this particular one uses asyncer to run the async function in a thread
we're going in with this with the hope that the official solution is
closer to this than a decorator per command.
"""

import asyncio
import inspect
from functools import partial, wraps
from typer import Typer
from typing import Any, Callable, TypeVar

F = TypeVar('F', bound=Callable[..., Any])


class AsyncTyper(Typer):
    @staticmethod
    def maybe_run_async(decorator, f):  # type: ignore
        if inspect.iscoroutinefunction(f):

            @wraps(f)
            def runner(*args, **kwargs):  # type: ignore
                return asyncio.run(f(*args, **kwargs))

            decorator(runner)
        else:
            decorator(f)
        return f

    def callback(self, *args, **kwargs) -> Callable[[F], F]:  # type: ignore
        decorator = super().callback(*args, **kwargs)
        return partial(self.maybe_run_async, decorator)

    def command(self, *args, **kwargs) -> Callable[[F], F]:  # type: ignore
        decorator = super().command(*args, **kwargs)
        return partial(self.maybe_run_async, decorator)
