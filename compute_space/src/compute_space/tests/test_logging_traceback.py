"""Verify that exception logging produces plain Python tracebacks.

Loguru's defaults (diagnose=True, backtrace=True) annotate tracebacks with the
values of local variables at each frame, which leaks secrets into logs. We
override those defaults in compute_space.core.logging — these tests guard that.
"""

import sys

from loguru import logger


def _trigger_exception_with_secret(secret: str) -> None:
    local_secret_var = secret  # noqa: F841 — present so loguru's diagnose mode would render it
    raise RuntimeError("boom")


def test_logger_opt_exception_does_not_leak_local_variables(capsys):
    # Importing the module configures loguru's sinks.
    import compute_space.core.logging  # noqa: F401, PLC0415

    secret = "SUPER_SECRET_VALUE_42"
    try:
        _trigger_exception_with_secret(secret)
    except RuntimeError as exc:
        logger.opt(exception=exc).error("Unhandled exception")

    captured = capsys.readouterr()
    output = captured.err + captured.out
    assert "RuntimeError" in output, "expected traceback to be logged"
    assert secret not in output, f"local variable value leaked into traceback output:\n{output}"


def test_std_logging_exception_does_not_leak_local_variables(capsys):
    """The std-logging interceptor routes through loguru; same guarantee applies."""
    import logging as stdlib_logging  # noqa: PLC0415

    import compute_space.core.logging  # noqa: F401, PLC0415

    secret = "ANOTHER_SECRET_VALUE_99"
    try:
        _trigger_exception_with_secret(secret)
    except RuntimeError:
        stdlib_logging.getLogger("test").exception("via std logging")

    captured = capsys.readouterr()
    output = captured.err + captured.out
    assert "RuntimeError" in output
    assert secret not in output, f"local variable value leaked via std-logging interceptor:\n{output}"


def test_default_loguru_config_would_leak(capsys):
    """Sanity check: confirm loguru's *default* config does leak the secret.

    This guards against the upstream defaults changing such that our test
    above passes trivially (i.e. tests would still pass even if our override
    were removed).
    """
    logger.remove()
    handler_id = logger.add(sys.stderr)  # defaults: backtrace=True, diagnose=True
    try:
        secret = "DEFAULT_CONFIG_SECRET_77"
        try:
            _trigger_exception_with_secret(secret)
        except RuntimeError as exc:
            logger.opt(exception=exc).error("default config")
        captured = capsys.readouterr()
        output = captured.err + captured.out
        assert secret in output, (
            "loguru's default config no longer leaks locals; the override in "
            "compute_space.core.logging may be unnecessary"
        )
    finally:
        logger.remove(handler_id)
        # Restore the module's configured sink for any subsequent tests.
        import importlib  # noqa: PLC0415

        import compute_space.core.logging as _logmod  # noqa: PLC0415

        importlib.reload(_logmod)
