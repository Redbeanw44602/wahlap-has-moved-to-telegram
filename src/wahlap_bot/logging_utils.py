"""Produce useful diagnostics without exception text, source code, or local variables."""

import errno
import sqlite3
import ssl
from datetime import timedelta
from pathlib import Path

import aiohttp
from telegram.error import RetryAfter


class DiagnosticError(Exception):
    """Keep a user-facing message separate from structured log diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "unspecified",
        http_status: int | None = None,
        api_code: int | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.http_status = http_status
        self.api_code = api_code


def _details(error: BaseException) -> str:
    fields = []
    if isinstance(error, DiagnosticError):
        fields.append(f"reason={error.reason}")
        if error.http_status is not None:
            fields.append(f"http_status={error.http_status}")
        if error.api_code is not None:
            fields.append(f"api_code={error.api_code}")
    if isinstance(error, aiohttp.ClientResponseError):
        fields.append(f"http_status={error.status}")
    if isinstance(error, OSError) and isinstance(error.errno, int):
        fields.append(f"errno={error.errno}({errno.errorcode.get(error.errno, 'UNKNOWN')})")
    if isinstance(error, ssl.SSLCertVerificationError) and hasattr(error, "verify_code"):
        fields.append(f"verify_code={error.verify_code}")
    if isinstance(error, sqlite3.Error) and hasattr(error, "sqlite_errorcode"):
        fields.append(f"sqlite_code={error.sqlite_errorcode}({error.sqlite_errorname})")
    if isinstance(error, RetryAfter):
        delay = error.retry_after
        seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
        fields.append(f"retry_after={seconds:.1f}s")
    return " " + " ".join(fields) if fields else ""


def describe_error(error: BaseException) -> str:
    chain = []
    traces = []
    seen = set()
    while error is not None and id(error) not in seen and len(chain) < 8:
        seen.add(id(error))
        name = type(error).__name__
        chain.append(name + _details(error))
        frames = []
        trace = error.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            frames.append(f"{Path(code.co_filename).name}:{trace.tb_lineno}({code.co_name})")
            trace = trace.tb_next
        if frames:
            traces.append(f"{name}: " + " > ".join(frames[-12:]))
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    result = " <- ".join(chain)
    if traces:
        result += " | stack=" + " | ".join(traces)
    return result
