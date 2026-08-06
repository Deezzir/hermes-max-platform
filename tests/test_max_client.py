import asyncio

import pytest
from aiohttp import web

from src.max_client import MaxApiError, MaxClient


async def test_send_message_uses_authorization_and_markdown(aiohttp_server):
    received = {}

    async def messages(request):
        received["authorization"] = request.headers["Authorization"]
        received["query"] = dict(request.query)
        received["body"] = await request.json()
        return web.json_response({"message": {"message_id": "m1"}})

    app = web.Application()
    app.router.add_post("/messages", messages)
    server = await aiohttp_server(app)
    client = MaxClient("secret", api_base=str(server.make_url("/")).rstrip("/"))

    result = await client.send_message(chat_id="42", text="**hello**")
    await client.close()

    assert result["message_id"] == "m1"
    assert received == {
        "authorization": "secret",
        "query": {"chat_id": "42"},
        "body": {"text": "**hello**", "format": "markdown"},
    }


async def test_retries_429_then_succeeds(aiohttp_server):
    calls = 0

    async def messages(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.json_response({"message": "slow down"}, status=429)
        return web.json_response({"message": {"message_id": "m1"}})

    app = web.Application()
    app.router.add_post("/messages", messages)
    server = await aiohttp_server(app)
    client = MaxClient(
        "secret",
        api_base=str(server.make_url("/")).rstrip("/"),
        sleep=lambda _: asyncio.sleep(0),
    )

    result = await client.send_message(chat_id="42", text="hello")
    await client.close()

    assert result["message_id"] == "m1"
    assert calls == 2


async def test_does_not_retry_authentication_failure(aiohttp_server):
    calls = 0

    async def messages(request):
        nonlocal calls
        calls += 1
        return web.json_response({"message": "invalid token"}, status=401)

    app = web.Application()
    app.router.add_post("/messages", messages)
    server = await aiohttp_server(app)
    client = MaxClient(
        "secret",
        api_base=str(server.make_url("/")).rstrip("/"),
        sleep=lambda _: asyncio.sleep(0),
    )

    with pytest.raises(MaxApiError, match="invalid token"):
        await client.send_message(chat_id="42", text="hello")
    await client.close()

    assert calls == 1
