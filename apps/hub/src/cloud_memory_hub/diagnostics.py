"""ADR-0041: dedicated-process output ownership before business imports.

Caller handlers are detached, never closed. Do not use this service factory in
a process whose other applications require a different logging policy.
"""

import logging
import sys
import threading
from contextlib import contextmanager

_LOCK = threading.RLock()
_OWNED = ("_hy_memory_diagnostic_handler", "_one_memory_diagnostic_handler")
# logging itself keeps only weak references. Retain detached caller objects so
# removing their last logger owner cannot indirectly close a caller's stream.
_detached_handlers = set()
_maintenance_thread = None


class _BootstrapFormatter(logging.Formatter):
    def format(self, record):
        return '{"content_omitted":true,"event":"diagnostic_bootstrap"}'


class _Console(logging.StreamHandler):
    def __init__(self):
        logging.Handler.__init__(self)

    @property
    def stream(self):
        # Resolve the current stderr, rather than retaining a caller's temporary
        # capture stream after that caller restores its logging configuration.
        return sys.stderr

    def handleError(self, record):
        # Default logging diagnostics print the original msg/args on I/O failure.
        # Losing this diagnostic is preferable to creating a new body copy.
        return


_console = _Console()


def _route(formatter):
    root = logging.getLogger()
    loggers = [root] + [
        item
        for item in tuple(logging.Logger.manager.loggerDict.values())
        if isinstance(item, logging.Logger)
    ]
    for logger in loggers:
        for handler in tuple(logger.handlers):
            if handler is _console or any(getattr(handler, flag, False) is True for flag in _OWNED):
                handler.setFormatter(formatter)
            else:
                _detached_handlers.add(handler)
                logger.removeHandler(handler)
        # SDK-owned handlers retain their request/level filters. Caller logger
        # callbacks do not belong to the dedicated service's output contract.
        logger.filters.clear()
        logger.disabled = False
        if logger is not root:
            logger.propagate = not any(
                getattr(handler, "_hy_memory_diagnostic_handler", False) is True
                for handler in logger.handlers
            )
    if _console not in root.handlers:
        root.addHandler(_console)
    _console.setFormatter(formatter)
    logging.lastResort = _console
    logging.raiseExceptions = False
    logging.captureWarnings(True)


def initialize_diagnostics():
    """Protect imports, activate the immutable native policy, then route output.

    Any active legacy native resource makes activation fail before business data
    is opened. The bootstrap still protects the resulting startup diagnostic.
    """
    with _LOCK:
        if _maintenance_thread == threading.get_ident():
            from hy_memory.utils.diagnostic_policy import DiagnosticPolicyError

            raise DiagnosticPolicyError("diagnostic_file_logging_busy")
        _route(_BootstrapFormatter())
        from hy_memory.utils.diagnostic_policy import (
            StrictDiagnosticFormatter,
            enable_strict_diagnostics,
        )

        enable_strict_diagnostics()
        _route(StrictDiagnosticFormatter())


@contextmanager
def diagnostic_file_maintenance(*, roots):
    """Synchronous routing/native-handler exclusion; caller owns worker draining.

    Hold this context through the entire joined file thread, never across await.
    Retained caller handlers remain caller-owned and are passed for conflict
    detection only, including descriptors detached during strict initialization.
    """
    from hy_memory.utils.diagnostic_policy import DiagnosticPolicyError
    from hy_memory.utils.file_log_lifecycle import diagnostic_file_maintenance as native_gate

    global _maintenance_thread
    if _LOCK._is_owned() or not _LOCK.acquire(blocking=False):
        raise DiagnosticPolicyError("diagnostic_file_logging_busy")
    try:
        _maintenance_thread = threading.get_ident()
        with native_gate(roots=roots, detached_handlers=tuple(_detached_handlers)) as lease:
            yield lease
    finally:
        _maintenance_thread = None
        _LOCK.release()


def install_process_exception_hooks():
    """Only the CLI owns these hooks; embedded factory calls do not replace them."""
    logger = logging.getLogger("cloud_memory_hub.diagnostics")

    def report(error_type, error, traceback):
        if error_type in (KeyboardInterrupt, SystemExit):
            return
        logger.error("hub_unhandled_exception", exc_info=(error_type, error, traceback))

    sys.excepthook = report
    threading.excepthook = lambda args: report(args.exc_type, args.exc_value, args.exc_traceback)
    sys.unraisablehook = lambda args: report(args.exc_type, args.exc_value, args.exc_traceback)
