"""Describe failures without logging exception messages or request data."""

from pathlib import Path


def describe_error(error: BaseException) -> str:
    names = [type(error).__name__]
    cause = error.__cause__
    while cause is not None and len(names) < 3:
        names.append(type(cause).__name__)
        cause = cause.__cause__
    result = " <- ".join(names)
    trace = error.__traceback__
    if trace is not None:
        while trace.tb_next is not None:
            trace = trace.tb_next
        code = trace.tb_frame.f_code
        result += f" at {Path(code.co_filename).name}:{trace.tb_lineno} ({code.co_name})"
    return result
