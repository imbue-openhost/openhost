import asyncio
import functools
import os
from collections.abc import Callable
from collections.abc import Coroutine
from pathlib import Path
from typing import Any


def write_restricted(path: Path, content: str) -> None:
    """Write a file that is only readable by the owner (mode 0o600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


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
