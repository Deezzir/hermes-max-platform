import asyncio
import logging
import sys
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from src.adapter import (
    MaxAdapter,
    _apply_yaml_config,
    _env_enablement,
    _media_type,
    _standalone_send,
    register,
    validate_config,
)
from src.event_mapper import InboundEvent


@pytest.fixture
def adapter_config(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "token")
    monkeypatch.setenv("MAX_WEBHOOK_URL", "https://webhook.url")
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


async def test_health_and_webhook_reject_invalid_payloads(adapter_config):
    adapter = MaxAdapter(adapter_config)
    client = TestClient(TestServer(adapter.create_app()))
    await client.start_server()

    assert await (await client.get("/health")).json() == {"status": "ok", "platform": "max"}
    response = await client.post("/", headers={"X-Max-Bot-Api-Secret": "webhook-secret"}, data="{")
    assert response.status == 400
    response = await client.post("/", headers={"X-Max-Bot-Api-Secret": "webhook-secret"}, json=[])
    assert response.status == 400
    await client.close()


async def test_webhook_deduplicates_identical_updates(adapter_config, message_created_update):
    adapter = MaxAdapter(adapter_config)
    adapter._process_update = AsyncMock()
    client = TestClient(TestServer(adapter.create_app()))
    await client.start_server()

    for _ in range(2):
        response = await client.post(
            "/", headers={"X-Max-Bot-Api-Secret": "webhook-secret"}, json=message_created_update
        )
        assert response.status == 200
    await asyncio.sleep(0)
    adapter._process_update.assert_awaited_once_with(message_created_update)
    await client.close()


async def test_process_update_handles_mentions_and_dispatches_media(adapter_config, monkeypatch):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    adapter._bot_username = "maxbot"
    adapter._require_mention = True
    adapter._inbound_media = AsyncMock(return_value=(["/cache/image.png"], ["image/png"]))
    adapter.handle_message = AsyncMock()
    update = {
        "update_type": "message_created",
        "message": {
            "message_id": "m1",
            "body": {"text": "@maxbot inspect this"},
            "sender": {"user_id": 7, "name": "Ada"},
            "recipient": {"chat_id": -42, "type": "chat"},
        },
    }

    await adapter._process_update(update)

    event = adapter.handle_message.await_args.args[0]
    assert event.text == "inspect this"
    assert event.media_urls == ["/cache/image.png"]
    assert event.message_type.value == "text"
    await adapter._process_update(
        {**update, "message": {**update["message"], "body": {"text": "ignore"}}}
    )
    assert adapter.handle_message.await_count == 1


async def test_process_update_rejects_unauthorized_users(adapter_config):
    adapter = MaxAdapter(adapter_config)
    adapter.handle_message = AsyncMock()
    await adapter._process_update(
        {
            "update_type": "message_created",
            "message": {
                "body": {"text": "hello"},
                "sender": {"user_id": 8, "name": "Grace"},
                "recipient": {"chat_id": 9},
            },
        }
    )
    adapter.handle_message.assert_not_awaited()


async def test_connect_rejects_missing_configuration(monkeypatch):
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    adapter = MaxAdapter(SimpleNamespace(extra={}))
    assert await adapter.connect() is False


async def test_connect_starts_listener_registers_subscription_and_disconnects(
    adapter_config, monkeypatch
):
    adapter = MaxAdapter(adapter_config)
    client = FakeClient()
    client.get_me = AsyncMock(return_value={"username": "maxbot"})
    client.register_subscription = AsyncMock()
    runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
    site = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    status = SimpleNamespace(
        acquire_scoped_lock=lambda platform, token: (True, None),
        release_scoped_lock=MagicMock(),
    )

    monkeypatch.setattr("src.adapter.MaxClient", lambda token: client)
    monkeypatch.setattr("src.adapter.web.AppRunner", lambda app: runner)
    monkeypatch.setattr("src.adapter.web.TCPSite", lambda runner, host, port: site)
    monkeypatch.setitem(sys.modules, "gateway.status", status)
    monkeypatch.setattr(adapter, "_register_commands", AsyncMock())

    assert await adapter.connect() is True
    assert adapter._bot_username == "maxbot"
    client.register_subscription.assert_awaited_once()
    await adapter.disconnect()
    site.stop.assert_awaited_once()
    runner.cleanup.assert_awaited_once()
    assert status.release_scoped_lock.call_count == 1


async def test_connect_marks_failure_when_max_setup_raises(adapter_config, monkeypatch):
    adapter = MaxAdapter(adapter_config)
    client = FakeClient()
    client.get_me = AsyncMock(side_effect=RuntimeError("MAX unavailable"))
    status = SimpleNamespace(
        acquire_scoped_lock=lambda platform, token: (True, None),
        release_scoped_lock=MagicMock(),
    )
    monkeypatch.setattr("src.adapter.MaxClient", lambda token: client)
    monkeypatch.setitem(sys.modules, "gateway.status", status)

    assert await adapter.connect() is False
    assert client.closed is True


async def test_register_commands_uses_available_hermes_commands(adapter_config, monkeypatch):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    command = SimpleNamespace(name="help", description="Help command")
    commands = SimpleNamespace(
        COMMAND_REGISTRY=[command],
        _is_gateway_available=lambda command, gates: True,
        _resolve_config_gates=lambda: {},
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.commands", commands)

    await adapter._register_commands()

    assert adapter._client.commands == [{"name": "help", "description": "Help command"}]


async def test_disconnect_closes_client_and_pending_tasks(adapter_config):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    task = asyncio.create_task(asyncio.sleep(60))
    adapter._tasks.add(task)

    await adapter.disconnect()

    assert task.cancelled()
    assert adapter._client is None


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


async def test_webhook_logs_unauthorized_sender_identity_without_message_text(
    adapter_config, message_created_update, caplog
):
    adapter = MaxAdapter(adapter_config)
    message_created_update["message"]["body"]["text"] = "private message"
    message_created_update["message"]["sender"]["user_id"] = 99
    message_created_update["message"]["sender"]["name"] = "Grace"
    client = TestClient(TestServer(adapter.create_app()))
    await client.start_server()

    response = await client.post(
        "/",
        headers={"X-Max-Bot-Api-Secret": "webhook-secret"},
        json=message_created_update,
    )
    await asyncio.sleep(0)

    assert response.status == 200
    assert "99" in caplog.text
    assert "Grace" in caplog.text
    assert "private message" not in caplog.text
    await client.close()
    await adapter.disconnect()


async def test_webhook_logs_accepted_update_without_message_text(
    adapter_config, message_created_update, caplog
):
    adapter = MaxAdapter(adapter_config)
    message_created_update["message"]["body"]["text"] = "private message"
    client = TestClient(TestServer(adapter.create_app()))
    await client.start_server()
    caplog.set_level(logging.INFO, logger="src.adapter")

    try:
        response = await client.post(
            "/",
            headers={"X-Max-Bot-Api-Secret": "webhook-secret"},
            json=message_created_update,
        )
        await asyncio.sleep(0)

        assert response.status == 200
        assert "message_created" in caplog.text
        assert "9" in caplog.text
        assert "private message" not in caplog.text
    finally:
        await client.close()
        await adapter.disconnect()


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


async def test_interactive_callback_rejects_another_group_member(adapter_config):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    adapter._allowed_users = {"7", "8"}
    called = False

    async def resolve(value):
        nonlocal called
        called = True
        return "approved"

    interaction_id = adapter._next_interaction("-42", "m1", resolve, user_id="7")
    event = InboundEvent("", "-42", "group", "8", "Grace", None, {})

    handled = await adapter._handle_interactive_callback(
        event, {"callback_id": "cb1", "payload": f"hermes:{interaction_id}:once"}
    )

    assert handled is True
    assert called is False
    assert adapter._client.answers == [("cb1", "This control has expired")]


async def test_webhook_passes_downloaded_image_to_hermes(adapter_config, monkeypatch, tmp_path):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    image = tmp_path / "image.png"
    buffer = BytesIO()
    Image.new("RGB", (1, 1)).save(buffer, format="PNG")
    image.write_bytes(buffer.getvalue())
    adapter._client.download_attachment = AsyncMock(return_value=(image.read_bytes(), "image/png"))
    adapter._client.downloaded_file = image
    monkeypatch.setattr("src.adapter.cache_image_from_bytes", lambda data, extension: str(image))
    received = asyncio.Event()

    async def handle_message(event):
        assert event.media_urls == [str(image)]
        assert event.media_types == ["image/png"]
        received.set()

    adapter.handle_message = handle_message
    client = TestClient(TestServer(adapter.create_app()))
    await client.start_server()

    response = await client.post(
        "/",
        headers={"X-Max-Bot-Api-Secret": "webhook-secret"},
        json={
            "update_type": "message_created",
            "message": {
                "message_id": "m1",
                "body": {
                    "attachments": [
                        {"type": "image", "payload": {"url": "https://cdn.max.ru/image.png"}}
                    ]
                },
                "sender": {"user_id": 7, "name": "Ada"},
                "recipient": {"chat_id": 9},
            },
        },
    )

    assert response.status == 200
    await asyncio.wait_for(received.wait(), timeout=1)
    await client.close()


async def test_inbound_media_caches_files_and_ignores_unknown_types(adapter_config, monkeypatch):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    monkeypatch.setattr(
        "src.adapter.cache_document_from_bytes", lambda data, name: f"/cache/{name}"
    )
    monkeypatch.setattr(
        "src.adapter.cache_audio_from_bytes", lambda data, extension: f"/cache/audio{extension}"
    )
    adapter._client.download_attachment = AsyncMock(
        side_effect=[(b"file", "text/plain"), (b"audio", "audio/ogg")]
    )

    urls, types = await adapter._inbound_media(
        {
            "message": {
                "body": {
                    "attachments": [
                        {
                            "type": "file",
                            "payload": {
                                "url": "https://fd.oneme.ru/file.txt",
                                "filename": "file.txt",
                            },
                        },
                        {"type": "voice", "payload": {"url": "https://fd.oneme.ru/voice.ogg"}},
                        {"type": "contact", "payload": {"url": "https://fd.oneme.ru/contact"}},
                    ]
                }
            }
        }
    )

    assert urls == ["/cache/file.txt", "/cache/audio.ogg"]
    assert types == ["text/plain", "audio/ogg"]


async def test_inbound_media_rejects_undecodable_image(adapter_config, monkeypatch):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    adapter._client.download_attachment = AsyncMock(
        return_value=(b"RIFF\x00\x00\x00\x00WEBP", "image/webp")
    )
    cache_image = MagicMock(return_value="/cache/image.webp")
    monkeypatch.setattr("src.adapter.cache_image_from_bytes", cache_image)

    urls, types = await adapter._inbound_media(
        {
            "message": {
                "body": {
                    "attachments": [
                        {"type": "image", "payload": {"url": "https://i.oneme.ru/image"}}
                    ]
                }
            }
        }
    )

    assert urls == []
    assert types == []
    cache_image.assert_not_called()


async def test_send_and_status_use_recipient_type(adapter_config):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    adapter._chat_types["7"] = "dm"

    result = await adapter.send("7", "hello", reply_to="reply")
    assert result.success is True
    assert adapter._client.messages[-1]["user_id"] == "7"
    assert "chat_id" not in adapter._client.messages[-1]

    result = await adapter.send_or_update_status("7", "status", "working")
    assert result.message_id == "m2"
    result = await adapter.send_or_update_status("7", "status", "finished")
    assert result.message_id == "m2"
    assert adapter._client.edits == [("m2", "finished", None)]


async def test_send_typing_and_file_send(adapter_config, tmp_path):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    adapter._chat_types["-42"] = "group"
    await adapter.send_typing("-42")
    assert adapter._client.actions == [("-42", "typing_on")]

    file_path = tmp_path / "file.txt"
    file_path.write_text("content")
    result = await adapter.send_document("-42", str(file_path), "caption", reply_to="reply")
    assert result.success is True
    assert adapter._client.uploads == [(file_path, "file")]
    assert adapter._client.messages[-1]["chat_id"] == "-42"


async def test_interactive_callbacks_resolve_and_edit_messages(adapter_config):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    adapter._allowed_users = {"7"}
    result = await adapter._send_interaction(
        "-42",
        "Choose",
        [("Yes", "yes")],
        AsyncMock(return_value="Selected"),
        user_id="7",
    )
    mapped = InboundEvent("", "-42", "group", "7", "Ada", None, {})

    assert result.success is True
    handled = await adapter._handle_interactive_callback(
        mapped, {"callback_id": "callback", "payload": "hermes:1:yes"}
    )
    assert handled is True
    assert adapter._client.answers[-1] == ("callback", None)
    assert adapter._client.edits[-1] == ("m1", "Selected", [])


async def test_interaction_rejects_unauthorized_and_expired_callbacks(adapter_config):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    adapter._allowed_users = {"7"}
    mapped = InboundEvent("", "-42", "group", "8", "Grace", None, {})

    assert await adapter._handle_interactive_callback(mapped, {"callback_id": "cb", "payload": "x"})
    assert adapter._client.answers == [("cb", "Not authorized")]
    adapter._allowed_users.add("8")
    assert await adapter._handle_interactive_callback(
        mapped, {"callback_id": "cb2", "payload": "hermes:missing:yes"}
    )
    assert adapter._client.answers[-1] == ("cb2", "This control has expired")


async def test_send_interaction_limits_buttons_and_updates_existing_message(adapter_config):
    adapter = MaxAdapter(adapter_config)
    adapter._client = FakeClient()
    handler = AsyncMock(return_value=None)

    result = await adapter._send_interaction("7", "too many", [("x", "x")] * 31, handler)
    assert result.success is False
    result = await adapter._send_interaction("7", "updated", [("x", "x")], handler, message_id="m1")
    assert result.success is True
    assert adapter._client.edits[-1][0:2] == ("m1", "updated")


async def test_choice_and_model_pickers_build_native_controls(adapter_config, monkeypatch):
    adapter = MaxAdapter(adapter_config)
    interactions = []

    async def send_interaction(*args, **kwargs):
        interactions.append((args, kwargs))
        return SimpleNamespace(success=True, message_id="m1")

    monkeypatch.setattr(adapter, "_send_interaction", send_interaction)
    chosen = AsyncMock(return_value="chosen")
    await adapter.send_choice_picker(
        "7", "Pick", [{"value": "a", "label": "A", "is_current": True}], "max:7:9", chosen
    )
    await adapter.send_model_picker(
        "7",
        [{"slug": "provider", "name": "Provider", "models": ["provider/model"]}],
        "model",
        "provider",
        "max:7:9",
        AsyncMock(return_value="selected"),
    )

    assert interactions[0][0][2] == [("✓ A", "a")]
    assert interactions[1][0][2] == [("✓ Provider", "provider")]


async def test_clarify_and_approval_controls_resolve_core_callbacks(adapter_config, monkeypatch):
    adapter = MaxAdapter(adapter_config)
    captured = []

    async def send_interaction(*args, **kwargs):
        captured.append(args)
        return SimpleNamespace(success=True, message_id="m1")

    monkeypatch.setattr(adapter, "_send_interaction", send_interaction)
    clarify = SimpleNamespace(mark_awaiting_text=MagicMock(), resolve_gateway_clarify=MagicMock())
    approval = SimpleNamespace(resolve_gateway_approval=lambda session, choice: choice == "once")
    slash = SimpleNamespace(resolve=AsyncMock(return_value="confirmed"))
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", clarify)
    monkeypatch.setitem(sys.modules, "tools.approval", approval)
    monkeypatch.setitem(sys.modules, "tools.slash_confirm", slash)

    await adapter.send_clarify("7", "Question", ["A"], "clarify", "max:7:9")
    clarify_handler = captured[-1][3]
    assert await clarify_handler("0") == "Selection received."
    clarify.resolve_gateway_clarify.assert_called_once_with("clarify", "A")
    assert await clarify_handler("other") == "Type your answer."
    await adapter.send_exec_approval("7", "rm", "max:7:9")
    approval_handler = captured[-1][3]
    assert await approval_handler("once") == "Approval received."
    await adapter.send_slash_confirm("7", "Title", "Message", "max:7:9", "confirm")
    assert await captured[-1][3]("once") == "confirmed"


async def test_model_page_selects_navigation_back_and_model(adapter_config, monkeypatch):
    adapter = MaxAdapter(adapter_config)
    interactions = []

    async def send_interaction(*args, **kwargs):
        interactions.append(args)
        return SimpleNamespace(success=True, message_id="m1")

    monkeypatch.setattr(adapter, "_send_interaction", send_interaction)
    selected = AsyncMock(return_value="selected")
    back = AsyncMock()
    await adapter._send_model_page(
        "7", "provider", [f"p/{index}" for index in range(10)], 0, selected, "m1", back
    )
    handler = interactions[0][3]
    assert await handler("model:0") == "selected"
    assert selected.await_args.args == ("7", "p/0", "provider")
    assert await handler("page:1") is None
    assert await handler("back") is None
    back.assert_awaited_once()


async def test_standalone_send_uploads_media_and_closes_client(monkeypatch, tmp_path):
    client = FakeClient()
    monkeypatch.setattr("src.adapter.MaxClient", lambda token: client)
    path = tmp_path / "image.jpg"
    path.write_bytes(b"image")

    result = await _standalone_send(
        SimpleNamespace(extra={"token": "token"}), "7", "hello", media_files=[str(path)]
    )

    assert result == {"success": True, "message_id": "m1"}
    assert client.uploads == [(path, "image")]
    assert client.closed is True


async def test_standalone_send_requires_token_and_reports_upload_errors(monkeypatch, tmp_path):
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    assert await _standalone_send(SimpleNamespace(extra={}), "7", "hello") == {
        "error": "MAX_BOT_TOKEN is not configured"
    }
    client = FakeClient()
    client.upload_file = AsyncMock(side_effect=RuntimeError("upload failed"))
    monkeypatch.setattr("src.adapter.MaxClient", lambda token: client)
    path = tmp_path / "file.txt"
    path.write_text("content")
    assert await _standalone_send(
        SimpleNamespace(extra={"token": "token"}), "7", "hello", media_files=[str(path)]
    ) == {"error": "upload failed"}


async def test_adapter_send_failure_and_helpers(adapter_config):
    adapter = MaxAdapter(adapter_config)
    result = await adapter.send("7", "hello")
    assert result.success is False
    assert result.error == "MAX adapter is not connected"
    adapter._client = FakeClient()
    adapter._client.send_message = AsyncMock(side_effect=RuntimeError("failed"))
    assert (await adapter.send("7", "hello")).error == "failed"
    assert await adapter.get_chat_info("7") == {"name": "7", "type": "dm"}
    assert adapter._split_text("") == [""]
    assert len(adapter._split_text("x" * 4001)) == 2


async def test_media_send_wrappers_delegate_to_send_file(adapter_config, tmp_path, monkeypatch):
    adapter = MaxAdapter(adapter_config)
    send_file = AsyncMock(return_value=SimpleNamespace(success=True, message_id="m1"))
    monkeypatch.setattr(adapter, "_send_file", send_file)

    await adapter.send_image_file("7", str(tmp_path / "image.png"), reply_to="reply")
    await adapter.send_voice("7", str(tmp_path / "voice.ogg"))
    await adapter.send_video("7", str(tmp_path / "video.mp4"))

    assert send_file.await_args_list[0].args[2] == "image"
    assert send_file.await_args_list[1].args[2] == "audio"
    assert send_file.await_args_list[2].args[2] == "video"


async def test_send_file_requires_connected_client(adapter_config, tmp_path):
    adapter = MaxAdapter(adapter_config)
    result = await adapter.send_document("7", str(tmp_path / "file.txt"))

    assert result.success is False
    assert result.error == "MAX adapter is not connected"


def test_adapter_configuration_helpers(adapter_config, monkeypatch):
    assert validate_config(adapter_config) is True
    assert _media_type(SimpleNamespace(suffix=".jpg")) == "image"
    assert _media_type(SimpleNamespace(suffix=".mp4")) == "video"
    assert _media_type(SimpleNamespace(suffix=".mp3")) == "audio"
    assert _media_type(SimpleNamespace(suffix=".txt")) == "file"
    assert _env_enablement()["allowed_users"] == "7"
    monkeypatch.delenv("MAX_REQUIRE_MENTION", raising=False)
    _apply_yaml_config({}, {"require_mention": True})
    assert _env_enablement() is not None
    assert validate_config(SimpleNamespace(extra={})) is True


class FakeClient:
    def __init__(self):
        self.messages = []
        self.actions = []
        self.uploads = []
        self.answers = []
        self.edits = []
        self.commands = []
        self.closed = False
        self.downloaded_file = None

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"message_id": f"m{len(self.messages)}"}

    async def send_action(self, chat_id, action="typing_on"):
        self.actions.append((chat_id, action))

    async def upload_file(self, path, media_type):
        self.uploads.append((path, media_type))
        return {"token": "uploaded"}

    async def answer_callback(self, callback_id, text=None):
        self.answers.append((callback_id, text))

    async def edit_message(self, message_id, text, attachments=None):
        self.edits.append((message_id, text, attachments))
        return {"message_id": message_id}

    async def download_attachment(self, url):
        return b"image", "image/png"

    async def close(self):
        self.closed = True

    async def set_commands(self, commands):
        self.commands = commands
