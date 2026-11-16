import asyncio
import stat

from wahlap_bot.endpoints import UserEndpoint
from wahlap_bot.storage import UserStore


async def test_isolation_persistence_update_and_delete(tmp_path):
    path = tmp_path / "private" / "users.sqlite3"
    store = UserStore(path)
    await store.open()
    try:
        await asyncio.gather(
            store.save(UserEndpoint(1001, "http://one.example", "first-secret")),
            store.save(UserEndpoint(1002, "http://two.example", "second-secret")),
        )
        assert (await store.get(1001)).auth_token == "first-secret"
        assert (await store.get(1002)).endpoint == "http://two.example"
        assert await store.get(9999) is None
    finally:
        await store.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    reopened = UserStore(path)
    await reopened.open()
    try:
        assert (await reopened.get(1002)).auth_token == "second-secret"
        await reopened.save(UserEndpoint(1001, "https://new.example"))
        assert (await reopened.get(1001)).auth_token is None
        await reopened.delete(1001)
        assert await reopened.get(1001) is None
        assert (await reopened.get(1002)).auth_token == "second-secret"
    finally:
        await reopened.close()
