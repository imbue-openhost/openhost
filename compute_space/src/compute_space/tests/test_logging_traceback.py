"""Verify that exception logging produces plain Python tracebacks.

Loguru's defaults (diagnose=True, backtrace=True) annotate tracebacks with the
values of local variables at each frame, which leaks secrets into logs. We
override those defaults in compute_space.core.logging — these tests guard that.
"""

import importlib
import logging as stdlib_logging

from loguru import logger

# Importing the module configures loguru's stderr sink with diagnose=False.
# We import here so we can also re-attach our test sink with the same options.
import compute_space.core.logging as _core_logging  # noqa: F401


def _trigger_exception_with_secret(secret: str) -> None:
    local_secret_var = secret  # noqa: F841 — present so loguru's diagnose mode would render it
    raise RuntimeError("boom")


def _capture_via_sink(diagnose: bool, backtrace: bool):
    """Attach a temporary loguru sink that appends to a list, returning (sink_id, captured)."""
    captured: list[str] = []
    sink_id = logger.add(
        lambda msg: captured.append(str(msg)),
        format="{message}\n{exception}",
        backtrace=backtrace,
        diagnose=diagnose,
    )
    return sink_id, captured


def test_logger_opt_exception_does_not_leak_local_variables():
    sink_id, captured = _capture_via_sink(diagnose=False, backtrace=False)
    try:
        secret = "SUPER_SECRET_VALUE_42"
        try:
            _trigger_exception_with_secret(secret)
        except RuntimeError as exc:
            logger.opt(exception=exc).error("Unhandled exception")
    finally:
        logger.remove(sink_id)

    output = "".join(captured)
    assert "RuntimeError" in output, "expected traceback to be logged"
    assert secret not in output, f"local variable value leaked into traceback output:\n{output}"


def test_std_logging_exception_does_not_leak_local_variables():
    """The std-logging interceptor routes through loguru; same guarantee applies."""
    # Other tests / pytest's logging plugin may have replaced the root logger's
    # handlers; reload to reinstall the InterceptHandler that routes std logging
    # through loguru.
    importlib.reload(_core_logging)
    sink_id, captured = _capture_via_sink(diagnose=False, backtrace=False)
    try:
        secret = "ANOTHER_SECRET_VALUE_99"
        try:
            _trigger_exception_with_secret(secret)
        except RuntimeError:
            stdlib_logging.getLogger("test").exception("via std logging")
    finally:
        logger.remove(sink_id)

    output = "".join(captured)
    assert "RuntimeError" in output
    assert secret not in output, f"local variable value leaked via std-logging interceptor:\n{output}"


def test_default_loguru_config_would_leak():
    """Sanity check: confirm loguru's *default* config does leak the secret.

    This guards against the upstream defaults changing such that our test
    above passes trivially (i.e. tests would still pass even if our override
    were removed).
    """
    sink_id, captured = _capture_via_sink(diagnose=True, backtrace=True)
    try:
        secret = "DEFAULT_CONFIG_SECRET_77"
        try:
            _trigger_exception_with_secret(secret)
        except RuntimeError as exc:
            logger.opt(exception=exc).error("default config")
    finally:
        logger.remove(sink_id)

    output = "".join(captured)
    assert secret in output, (
        "loguru with diagnose=True no longer leaks locals; the override in "
        "compute_space.core.logging may be unnecessary"
    )
