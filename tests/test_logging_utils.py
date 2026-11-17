import errno
import sqlite3
from types import SimpleNamespace

from aiohttp import ClientResponseError

from wahlap_bot.logging_utils import describe_error
from wahlap_bot.maiserver import _http_error, _unwrap


def inner_failure():
    raise OSError(errno.ECONNREFUSED, "private token", "/private/credential-file")


def outer_failure():
    try:
        inner_failure()
    except OSError as cause:
        raise RuntimeError("private url") from cause


def test_error_description_keeps_types_and_location_without_exception_data():
    try:
        try:
            raise OSError("https://private.example/?token=secret")
        except OSError as cause:
            raise RuntimeError("private user data") from cause
    except RuntimeError as error:
        summary = describe_error(error)
    assert "RuntimeError <- OSError" in summary
    assert "test_logging_utils.py:" in summary
    assert "private" not in summary and "secret" not in summary


def test_unraised_error_has_no_traceback():
    assert describe_error(ValueError("secret")) == "ValueError"


def test_diagnostics_preserve_cause_frames_and_network_error_code():
    try:
        outer_failure()
    except RuntimeError as error:
        summary = describe_error(error)
    assert "ECONNREFUSED" in summary
    assert "(inner_failure)" in summary and "(outer_failure)" in summary
    assert " > " in summary
    assert "private" not in summary and "credential" not in summary


def test_http_status_is_preserved_without_response_headers_or_url():
    error = ClientResponseError(
        SimpleNamespace(real_url="https://private.example/?token=secret"),
        (),
        status=403,
        message="private-body",
        headers={"X-Auth-Token": "secret"},
    )
    summary = describe_error(error)
    assert "http_status=403" in summary
    assert "private" not in summary and "secret" not in summary


def test_sqlite_error_code_is_preserved_without_sql_or_values():
    with sqlite3.connect(":memory:") as connection:
        try:
            connection.execute("SELECT * FROM private_table")
        except sqlite3.Error as error:
            summary = describe_error(error)
    assert "sqlite_code=1(SQLITE_ERROR)" in summary
    assert "private_table" not in summary


def test_implicit_context_is_reported_and_suppressed_context_is_respected():
    for suppressed in (False, True):
        try:
            try:
                raise ValueError("secret")
            except ValueError:
                if suppressed:
                    raise RuntimeError("secret") from None
                raise RuntimeError("secret")  # noqa: B904 - exercise implicit exception context
        except RuntimeError as error:
            summary = describe_error(error)
        assert ("ValueError" in summary) is not suppressed
        assert "secret" not in summary


def test_exception_cycles_are_bounded():
    first, second = ValueError("secret"), RuntimeError("secret")
    first.__cause__, second.__cause__ = second, first
    assert describe_error(first) == "ValueError <- RuntimeError"


def test_maiserver_http_errors_have_english_reasons_and_status_codes():
    summary = describe_error(_http_error(401))
    assert "reason=authentication_failed http_status=401" in summary
    assert summary.isascii()


def test_maiserver_api_errors_keep_codes_and_discard_server_messages():
    try:
        _unwrap({"code": 6, "message": "private-token-and-url"})
    except Exception as error:
        summary = describe_error(error)
    assert "reason=api_error api_code=6" in summary
    assert "private" not in summary and summary.isascii()
