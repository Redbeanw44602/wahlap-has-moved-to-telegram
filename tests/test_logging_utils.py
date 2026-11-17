from wahlap_bot.logging_utils import describe_error


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
