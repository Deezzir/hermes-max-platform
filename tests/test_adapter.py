import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from max_hermes_plugin.adapter import MaxAdapter, register


@pytest.fixture
def adapter_config(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "token")
    monkeypatch.setenv("MAX_WEBHOOK_URL", "https://max-webhook.bimos.noxu.dev/")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("MAX_ALLOWED_USERS", "7")
    return SimpleNamespace(extra={})


@pytest.fixture(autouse=True)
def register_max_platform():
    register(RecordingContext())


class RecordingContext:
    def register_platform(self, **kwargs):
        from gateway.platform_registry import PlatformEntry, platform_registry

        platform_registry.register(
            PlatformEntry(
                name=kwargs["name"],
                label=kwargs["label"],
                adapter_factory=kwargs["adapter_factory"],
                check_fn=kwargs["check_fn"],
            )
        )


@pytest.fixture
def message_created_update():
    return {
        "update_type": "message_created",
        "chat_id": 9,
        "message": {
            "message_id": "m1",
            "body": {"text": "hello"},
            "sender": {"user_id": 7, "name": "Ada"},
            "recipient": {"chat_id": 9},
        },
    }


async def test_webhook_rejects_bad_secret(adapter_config):
    adapter = MaxAdapter(adapter_config)
    client = TestClient(TestServer(adapter.create_app()))
    await client.start_server()

    response = await client.post("/", json={"update_type": "bot_started"})

    assert response.status == 401
    await client.close()


async def test_webhook_acknowledges_and_dispatches_valid_event(
    adapter_config, message_created_update
):
    adapter = MaxAdapter(adapter_config)
    received = asyncio.Event()

    async def handle_message(event):
        assert event.text == "hello"
        received.set()

    adapter.handle_message = handle_message
    client = TestClient(TestServer(adapter.create_app()))
    await client.start_server()

    response = await client.post(
        "/",
        headers={"X-Max-Bot-Api-Secret": "webhook-secret"},
        json=message_created_update,
    )

    assert response.status == 200
    await asyncio.wait_for(received.wait(), timeout=1)
    await client.close()


async def test_send_chunks_at_max_limit_and_preserves_reply(adapter_config):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    adapter._chat_types["42"] = "group"

    result = await adapter.send("42", "x" * 4001, reply_to="m0")

    assert result.success is True
    assert [call["text"] for call in adapter._client.messages] == ["x" * 4000, "x"]
    assert adapter._client.messages[0]["reply_to"] == "m0"


async def test_send_typing_is_noop_for_direct_messages(adapter_config):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    adapter._chat_types["7"] = "dm"

    await adapter.send_typing("7")

    assert adapter._client.actions == []


async def test_send_image_file_uploads_then_sends_attachment(adapter_config, tmp_path):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    adapter._chat_types["42"] = "group"
    image = tmp_path / "image.png"
    image.write_bytes(b"png")

    await adapter.send_image_file("42", str(image), caption="image")

    assert adapter._client.uploads == [(image, "image")]
    assert adapter._client.messages[-1]["attachments"] == [
        {"type": "image", "payload": {"token": "uploaded"}}
    ]


class FakeClient:
    def __init__(self):
        self.messages = []
        self.actions = []
        self.uploads = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"message_id": f"m{len(self.messages)}"}

    async def send_action(self, chat_id, action="typing_on"):
        self.actions.append((chat_id, action))

    async def upload_file(self, path, media_type):
        self.uploads.append((path, media_type))
        return {"token": "uploaded"}
