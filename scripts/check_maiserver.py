"""Exercise real maiserver responses without displaying account credentials."""

import argparse
import asyncio
import os
from urllib.parse import urlsplit

from wahlap_bot.endpoints import UserEndpoint, normalize_endpoint, validate_auth_token
from wahlap_bot.maiserver import MaiserverClient, MaiserverError, create_api_session


async def check(endpoint: str) -> None:
    token = os.environ.get("MAISERVER_AUTH_TOKEN")
    user = UserEndpoint(
        0, normalize_endpoint(endpoint), validate_auth_token(token) if token else None
    )
    async with create_api_session() as session:
        client = MaiserverClient(session)
        qr = await client.get_qrcode(user)
        with qr.png() as image:
            print(f"玩家二维码：成功（PNG {image.getbuffer().nbytes} 字节）")
        url = await client.get_net_url(user)
        print(f"maimai DX NET：成功（{urlsplit(url).hostname}；未访问授权链接）")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    try:
        asyncio.run(check(args.endpoint))
    except (MaiserverError, ValueError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
