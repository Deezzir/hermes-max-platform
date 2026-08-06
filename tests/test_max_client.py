import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from src.max_client import MaxApiError, MaxClient, _is_attachment_host


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("fd.oneme.ru", True),
        ("i.oneme.ru", True),
        ("cdn.max.ru", True),
        ("example.com", False),
        ("oneme.ru.example.com", False),
    ],
)
def test_attachment_host_allows_max_cdn_hosts(hostname, expected):
    assert _is_attachment_host(hostname) is expected


async def test_send_message_uses_authorization_and_markdown(aiohttp_server):
    received = {}

    async def messages(request):
        received["authorization"] = request.headers["Authorization"]
        received["query"] = dict(request.query)
        received["body"] = await request.json()
        return web.json_response({"message": {"body": {"mid": "m1"}}})

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


async def test_retries_transient_server_failure(aiohttp_server):
    calls = 0

    async def messages(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.json_response({"message": "temporary failure"}, status=502)
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


async def test_client_operations_serialize_expected_requests(aiohttp_server):
    requests = []

    async def handler(request):
        body = await request.read()
        requests.append(
            (
                request.method,
                request.path,
                dict(request.query),
                await request.json() if body else {},
            )
        )
        return web.json_response({"message": {"body": {"mid": "m1"}}})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = await aiohttp_server(app)
    client = MaxClient("secret", api_base=str(server.make_url("/")).rstrip("/"))

    assert await client.get_me() == {"message": {"body": {"mid": "m1"}}}
    await client.set_commands([{"name": "help", "description": "Help"}])
    await client.register_subscription("https://webhook.example", "secret", ["message_created"])
    await client.edit_message("m1", "updated", [])
    await client.send_action("42")
    await client.answer_callback("callback", "Done")
    await client.close()

    assert requests == [
        ("GET", "/me", {}, {}),
        ("PATCH", "/me/commands", {}, {"commands": [{"name": "help", "description": "Help"}]}),
        (
            "POST",
            "/subscriptions",
            {},
            {
                "url": "https://webhook.example",
                "secret": "secret",
                "update_types": ["message_created"],
            },
        ),
        (
            "PUT",
            "/messages",
            {"message_id": "m1"},
            {"text": "updated", "format": "markdown", "attachments": []},
        ),
        ("POST", "/chats/42/actions", {}, {"action": "typing_on"}),
        ("POST", "/answers", {"callback_id": "callback"}, {"notification": "Done"}),
    ]


async def test_send_message_normalizes_nested_message_id_and_validates_length(aiohttp_server):
    async def messages(request):
        return web.json_response({"message": {"body": {"mid": "nested"}}})

    app = web.Application()
    app.router.add_post("/messages", messages)
    server = await aiohttp_server(app)
    client = MaxClient("secret", api_base=str(server.make_url("/")).rstrip("/"))

    assert await client.send_message(user_id="42", text="hello", reply_to="reply") == {
        "body": {"mid": "nested"},
        "message_id": "nested",
    }
    with pytest.raises(ValueError, match="4000"):
        await client.send_message(text="x" * 4001)
    await client.close()


async def test_send_message_includes_attachments_and_direct_message_id(aiohttp_server):
    received = {}

    async def messages(request):
        received["body"] = await request.json()
        return web.json_response({"message_id": "direct"})

    app = web.Application()
    app.router.add_post("/messages", messages)
    server = await aiohttp_server(app)
    client = MaxClient("secret", api_base=str(server.make_url("/")).rstrip("/"))

    assert await client.send_message(chat_id="42", attachments=[{"type": "file"}]) == {
        "message_id": "direct"
    }
    assert received["body"] == {"attachments": [{"type": "file"}]}
    await client.close()


async def test_request_converts_network_errors_to_retryable_api_error(monkeypatch):
    client = MaxClient("secret", sleep=lambda _: asyncio.sleep(0))
    context = MagicMock()
    context.__aenter__.side_effect = asyncio.TimeoutError()
    monkeypatch.setattr(client._session, "request", lambda *args, **kwargs: context)

    with pytest.raises(MaxApiError) as error:
        await client.get_me()
    assert error.value.status == 503
    await client.close()


async def test_download_attachment_validates_url_and_returns_content_type(monkeypatch):
    client = MaxClient("secret")
    response = MagicMock()
    response.headers = {"Content-Length": "3", "Content-Type": "image/png; charset=binary"}
    response.content.read = AsyncMock(return_value=b"png")
    response.raise_for_status = MagicMock()
    context = MagicMock()
    context.__aenter__.return_value = response
    monkeypatch.setattr(client._session, "get", lambda *args, **kwargs: context)

    assert await client.download_attachment("https://i.oneme.ru/image") == (b"png", "image/png")
    with pytest.raises(ValueError, match="HTTPS"):
        await client.download_attachment("https://example.com/image")
    await client.close()


async def test_download_attachment_rejects_large_content(monkeypatch):
    client = MaxClient("secret")
    response = MagicMock()
    response.headers = {"Content-Length": str(10 * 1024 * 1024 + 1)}
    context = MagicMock()
    context.__aenter__.return_value = response
    monkeypatch.setattr(client._session, "get", lambda *args, **kwargs: context)

    with pytest.raises(ValueError, match="10 MiB"):
        await client.download_attachment("https://fd.oneme.ru/file")
    await client.close()


async def test_upload_file_posts_uploaded_data(aiohttp_server, tmp_path):
    uploaded = {}

    async def create_upload(request):
        return web.json_response({"url": str(request.url.with_path("/upload"))})

    async def upload(request):
        data = await request.post()
        uploaded["name"] = data["data"].filename
        uploaded["data"] = data["data"].file.read()
        return web.json_response({"token": "uploaded"})

    app = web.Application()
    app.router.add_post("/uploads", create_upload)
    app.router.add_post("/upload", upload)
    server = await aiohttp_server(app)
    path = tmp_path / "document.txt"
    path.write_text("content")
    client = MaxClient("secret", api_base=str(server.make_url("/")).rstrip("/"))

    assert await client.upload_file(Path(path), "file") == {"token": "uploaded"}
    assert uploaded == {"name": "document.txt", "data": b"content"}
    await client.close()
