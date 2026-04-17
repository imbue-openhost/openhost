import asyncio
import functools
from collections.abc import Callable
from collections.abc import Coroutine
from typing import Any


def assert_type[T](value: Any, expected_type: type[T]) -> T:
    if not isinstance(value, expected_type):
        raise TypeError(f"expected type {expected_type.__name__}, got {type(value).__name__}")
    return value


def assert_str(value: Any) -> str:
    return assert_type(value, str)


def assert_int(value: Any) -> int:
    return assert_type(value, int)


def async_wrap[T, **P](func: Callable[P, T]) -> Callable[P, Coroutine[Any, Any, T]]:
    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper
