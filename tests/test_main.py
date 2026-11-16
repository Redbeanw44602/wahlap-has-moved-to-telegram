from unittest.mock import Mock

import pytest
from telegram import Update
from telegram.error import RetryAfter

from wahlap_bot import __main__ as entry
from wahlap_bot.config import Settings


@pytest.fixture
def launcher(monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    monkeypatch.setattr(entry, "load_dotenv", Mock())
    monkeypatch.setattr(entry.Settings, "from_env", Mock(return_value=Settings(bot_token="test")))
    monkeypatch.setattr(entry.logging, "basicConfig", Mock())
    loop = Mock()
    loop.is_closed.return_value = False
    monkeypatch.setattr(entry.asyncio, "new_event_loop", Mock(return_value=loop))
    monkeypatch.setattr(entry.asyncio, "set_event_loop", Mock())
    application = Mock()
    monkeypatch.setattr(entry, "create_application", Mock(return_value=application))
    return application, loop


def test_polling_bootstrap_has_bounded_retries(launcher):
    app, loop = launcher
    entry.main()
    app.run_polling.assert_called_once_with(
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], bootstrap_retries=3
    )
    loop.close.assert_called_once()


def test_remaining_startup_rate_limit_reports_wait_time_instead_of_generic_configuration_error(
    launcher, caplog
):
    app, loop = launcher
    app.run_polling.side_effect = RetryAfter(23)
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    assert "限流" in caplog.text and "23.5" in caplog.text
    assert "网关配置" not in caplog.text
    loop.close.assert_called_once()
