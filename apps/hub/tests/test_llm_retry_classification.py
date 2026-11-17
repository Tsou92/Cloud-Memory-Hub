"""Transport failures must be retried by type, not by their message text."""

import httpx
import pytest
from hy_memory.agent.llm_provider import _is_retryable_llm_error


class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_dns_style_connect_error_is_retryable():
    # The message says nothing about "connection"; the type is the signal.
    error = httpx.ConnectError("[Errno 8] nodename nor servname provided")
    assert str(error).lower().find("connection") == -1
    assert _is_retryable_llm_error(error) is True


def test_transport_and_timeout_families_are_retryable():
    for error in (
        httpx.ConnectTimeout("operation timed out"),
        httpx.ReadTimeout("read timeout"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.PoolTimeout("pool exhausted"),
        TimeoutError(),
        ConnectionError("reset"),
    ):
        assert _is_retryable_llm_error(error) is True, type(error).__name__


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_transient_http_statuses_are_retryable(status):
    assert _is_retryable_llm_error(_StatusError(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_request_errors_are_not_retried(status):
    assert _is_retryable_llm_error(_StatusError(status)) is False


def test_unrelated_exception_is_not_retried():
    assert _is_retryable_llm_error(ValueError("bad prompt")) is False
