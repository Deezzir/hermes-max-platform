# MAX Hermes Plugin Implementation Plan

**Goal:** Build an installable, webhook-only Hermes platform plugin that exchanges authorized MAX messages, callbacks, and media with Hermes through MAX's REST API.

**Architecture:** The plugin owns a local aiohttp listener at `POST /`, validates and quickly acknowledges MAX webhooks, then asynchronously converts supported updates into Hermes `MessageEvent`s. A shared `MaxClient` owns MAX REST calls, retry/rate limiting, uploads, and sends; `MaxAdapter` owns the Hermes lifecycle, local listener, authorization, per-chat pacing, and native media overrides. Public TLS and forwarding to the listener remain deployment infrastructure, outside the plugin.

**Tech Stack:** Python 3.11+, Hermes Agent plugin API, `aiohttp`, `pytest`, `pytest-asyncio`.

**Constraints:** Do not alter Hermes core. Do not add polling. Require `MAX_ALLOWED_USERS`; do not implement allow-all access. Always send text as MAX Markdown. Do not commit changes; leave them for user review.

---

## File Structure

- Create: `pyproject.toml` - package metadata, runtime/test dependencies, pytest configuration.
- Create: `plugin.yaml` - Hermes plugin metadata and configuration UI entries.
- Create: `adapter.py` - `MaxAdapter`, plugin registration, listener lifecycle, webhook dispatch, Hermes media overrides, cron sender.
- Create: `max_client.py` - MAX API client, typed API errors, retry/rate-limit logic, message and upload calls.
- Create: `event_mapper.py` - pure MAX update parsing, message conversion, callback normalization, attachment mapping, keyboard conversion.
- Create: `tests/conftest.py` - isolated fake gateway modules and plugin loader.
- Create: `tests/test_event_mapper.py` - pure mapping and keyboard contract tests.
- Create: `tests/test_max_client.py` - REST payload, retry, and upload tests using aiohttp test server.
- Create: `tests/test_adapter.py` - configuration, webhook security, lifecycle, send pacing, media, and cron integration tests.
- Create: `README.md` - install, configuration, Cloudflare Tunnel/kgateway deployment wiring, and MAX prerequisites.
- Modify: `docs/design.md` - reconcile any implementation decisions uncovered by tests, without expanding scope.

## MAX-Specific Metadata Contract

Hermes does not expose a generic inline-keyboard API. The plugin accepts only this adapter-specific `metadata["max_keyboard"]` form on outbound sends:

```python
{
    "max_keyboard": [
        [{"type": "callback", "text": "Continue", "payload": "continue"}],
        [{"type": "link", "text": "Docs", "url": "https://example.com"}],
    ]
}
```

Supported button types are `callback`, `link`, `message`, `clipboard`, `request_contact`, `request_geo_location`, and `open_app`; each is validated against MAX's documented required fields and row limits. `message_callback` updates become text events in this stable form:

```text
[MAX callback payload: <payload>]
```

The original callback update remains in `MessageEvent.raw_message`. This preserves agent visibility without core changes.

### Task 1: Establish Package And Plugin Metadata

**Files:**
- Create: `pyproject.toml`
- Create: `plugin.yaml`
- Create: `tests/conftest.py`
- Test: `tests/test_adapter.py`

- [ ] **Step 1: Write the failing metadata test**

```python
def test_plugin_metadata_requires_token_url_secret_and_allowlist():
    metadata = yaml.safe_load(Path("plugin.yaml").read_text(encoding="utf-8"))
    required = {item["name"] for item in metadata["requires_env"]}

    assert metadata["kind"] == "platform"
    assert required == {
        "MAX_BOT_TOKEN",
        "MAX_WEBHOOK_URL",
        "MAX_WEBHOOK_SECRET",
        "MAX_ALLOWED_USERS",
    }
    assert "MAX_ALLOW_ALL_USERS" not in str(metadata)
    assert "MAX_OUTBOUND_FORMAT" not in str(metadata)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_adapter.py::test_plugin_metadata_requires_token_url_secret_and_allowlist -v`

Expected: FAIL because `plugin.yaml` does not exist.

- [ ] **Step 3: Create package metadata and plugin metadata**

Create `pyproject.toml` with Python `>=3.11`, runtime dependency `aiohttp>=3.10,<4`, dev dependencies `pytest` and `pytest-asyncio`, and `asyncio_mode = "auto"` under pytest options. Create `plugin.yaml` with:

```yaml
name: max-platform
label: MAX
kind: platform
version: 0.1.0
description: MAX Messenger gateway adapter for Hermes Agent.
author: Deezzir
requires_env:
  - name: MAX_BOT_TOKEN
    description: Bot API token from MAX Partner Platform.
    prompt: MAX bot token
    password: true
  - name: MAX_WEBHOOK_URL
    description: Public HTTPS URL forwarded by deployment infrastructure to this plugin.
    prompt: Public webhook URL
    password: false
  - name: MAX_WEBHOOK_SECRET
    description: Secret verified from X-Max-Bot-Api-Secret.
    prompt: Webhook secret
    password: true
  - name: MAX_ALLOWED_USERS
    description: Comma-separated MAX user IDs allowed to use Hermes.
    prompt: Allowed MAX user IDs
    password: false
optional_env:
  - name: MAX_LISTEN_HOST
    description: Local webhook listener host (default 0.0.0.0).
    prompt: Local listener host
    password: false
  - name: MAX_LISTEN_PORT
    description: Local webhook listener port (default 8080).
    prompt: Local listener port
    password: false
  - name: MAX_HOME_CHANNEL
    description: Default MAX chat or channel ID for cron delivery.
    prompt: MAX home channel
    password: false
  - name: MAX_REQUIRE_MENTION
    description: Require a mention before responding in group chats.
    prompt: Require mention in groups
    password: false
```

Create `tests/conftest.py` to inject minimal fake `gateway.config`, `gateway.platforms.base`, and `gateway.status` modules before importing plugin code. The fake base supplies `BasePlatformAdapter`, `MessageEvent`, `MessageType`, `SendResult`, and cache helpers with the same signatures used by the adapter.

- [ ] **Step 4: Run the metadata test to verify it passes**

Run: `uv run pytest tests/test_adapter.py::test_plugin_metadata_requires_token_url_secret_and_allowlist -v`

Expected: PASS.

### Task 2: Implement Pure MAX Update And Keyboard Mapping

**Files:**
- Create: `event_mapper.py`
- Create: `tests/test_event_mapper.py`

- [ ] **Step 1: Write failing conversion tests**

```python
def test_message_created_maps_direct_message():
    event = map_update({
        "update_type": "message_created",
        "chat_id": 9,
        "message": {
            "body": {"text": "hello"},
            "sender": {"user_id": 7, "name": "Ada"},
            "recipient": {"chat_id": 9},
        },
    })

    assert event.text == "hello"
    assert event.chat_id == "9"
    assert event.user_id == "7"
    assert event.chat_type == "dm"


def test_callback_maps_to_tagged_text():
    event = map_update({
        "update_type": "message_callback",
        "callback": {"payload": "continue"},
        "chat_id": 9,
        "user": {"user_id": 7, "name": "Ada"},
    })

    assert event.text == "[MAX callback payload: continue]"


def test_keyboard_rejects_invalid_callback_without_payload():
    with pytest.raises(ValueError, match="payload"):
        build_keyboard([[{"type": "callback", "text": "Continue"}]])
```

- [ ] **Step 2: Run mapper tests to verify they fail**

Run: `uv run pytest tests/test_event_mapper.py -v`

Expected: FAIL because `event_mapper` does not exist.

- [ ] **Step 3: Implement the mapper**

Implement dataclasses `InboundEvent` and `MappedAttachment`; pure functions `map_update(update)`, `event_fingerprint(update)`, and `build_keyboard(rows)`.

`map_update` must:

```python
if update_type == "bot_started":
    text = f"[MAX bot started: {payload}]" if payload else "[MAX bot started]"
elif update_type == "message_callback":
    text = f"[MAX callback payload: {payload}]"
elif update_type in {"message_created", "message_edited"}:
    text = message["body"].get("text") or attachment_summary(attachments)
else:
    return None
```

Use `is_channel` and recipient fields to classify `dm`, `group`, or `channel`; preserve original input as `raw_update`. Map attachments to names/type/payload only, and represent contact attachments as `[MAX contact redacted]` without retaining phone or vCard fields in the text payload.

`build_keyboard` must allow no more than 30 rows and 7 buttons per ordinary row, enforce 3-button rows when a link, open-app, contact, or geolocation button occurs, and create MAX's `{"type":"inline_keyboard","payload":{"buttons": rows}}` attachment.

- [ ] **Step 4: Run mapper tests to verify they pass**

Run: `uv run pytest tests/test_event_mapper.py -v`

Expected: PASS.

### Task 3: Implement MAX REST Client And Bounded Retry

**Files:**
- Create: `max_client.py`
- Create: `tests/test_max_client.py`

- [ ] **Step 1: Write failing API-client tests**

```python
async def test_send_message_uses_authorization_and_markdown(api_server):
    client = MaxClient("secret", api_base=api_server.url)

    await client.send_message(chat_id="42", text="**hello**")

    request = await api_server.next_request()
    assert request.headers["Authorization"] == "secret"
    assert request.query["chat_id"] == "42"
    assert request.json == {"text": "**hello**", "format": "markdown"}


async def test_retries_429_then_succeeds(api_server):
    api_server.queue(429, {"message": "slow down"})
    api_server.queue(200, {"message": {"message_id": "m1"}})
    client = MaxClient("secret", api_base=api_server.url, sleep=no_sleep)

    result = await client.send_message(chat_id="42", text="hello")

    assert result["message_id"] == "m1"
    assert api_server.request_count == 2
```

- [ ] **Step 2: Run client tests to verify they fail**

Run: `uv run pytest tests/test_max_client.py -v`

Expected: FAIL because `max_client` does not exist.

- [ ] **Step 3: Implement `MaxClient`**

Implement one `aiohttp.ClientSession` per client with `Authorization: <token>` and `https://platform-api2.max.ru` default base URL. Define `MaxApiError(status, message, retryable)` and methods:

```python
async def get_me() -> dict: ...
async def register_subscription(url: str, secret: str, update_types: list[str]) -> dict: ...
async def list_subscriptions() -> list[dict]: ...
async def delete_subscription(url: str) -> None: ...
async def send_message(*, chat_id: str | None, user_id: str | None,
                       text: str | None, attachments: list[dict] | None,
                       reply_to: str | None) -> dict: ...
async def send_action(chat_id: str, action: str = "typing_on") -> None: ...
async def answer_callback(callback_id: str, text: str | None = None) -> None: ...
async def upload_file(path: Path, media_type: str) -> dict: ...
```

Always set outbound text `format` to `markdown`. Reject text over 4,000 characters at this layer. Retry only network failures, `429`, `503`, and `attachment.not.ready`, at most three attempts with bounded exponential delay. Never retry `400`, `401`, or `404`. Use an `asyncio.Lock` and monotonic timestamps to maintain a 30-RPS shared minimum interval.

For uploads, request `/uploads?type=<type>`, multipart POST field `data` to the returned URL, and return the upload response payload for message attachments.

- [ ] **Step 4: Run client tests to verify they pass**

Run: `uv run pytest tests/test_max_client.py -v`

Expected: PASS.

### Task 4: Build Webhook Lifecycle And Inbound Dispatch

**Files:**
- Create: `adapter.py`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing listener and security tests**

```python
async def test_webhook_rejects_bad_secret(adapter, aiohttp_client):
    app = await adapter.start_listener()
    client = await aiohttp_client(app)

    response = await client.post("/", json={"update_type": "bot_started"})

    assert response.status == 401


async def test_webhook_acknowledges_and_dispatches_valid_event(adapter, aiohttp_client):
    received = asyncio.Event()
    adapter.handle_message = lambda event: received.set()
    app = await adapter.start_listener()
    client = await aiohttp_client(app)

    response = await client.post(
        "/",
        headers={"X-Max-Bot-Api-Secret": "webhook-secret"},
        json=message_created_update(),
    )

    assert response.status == 200
    await asyncio.wait_for(received.wait(), timeout=1)
```

- [ ] **Step 2: Run listener tests to verify they fail**

Run: `uv run pytest tests/test_adapter.py -k 'webhook' -v`

Expected: FAIL because `MaxAdapter` does not exist.

- [ ] **Step 3: Implement configuration, listener, and dispatch**

Implement `MaxAdapter(BasePlatformAdapter)` with configuration sourced from scoped secrets/environment first and `config.extra` second:

```python
MAX_BOT_TOKEN
MAX_WEBHOOK_URL
MAX_WEBHOOK_SECRET
MAX_ALLOWED_USERS
MAX_LISTEN_HOST=0.0.0.0
MAX_LISTEN_PORT=8080
```

Require every required value and a nonempty numeric allowlist. Do not read or honor an allow-all variable.

`connect()` must acquire a scoped token lock, construct `MaxClient`, call `get_me()`, start an aiohttp listener with only `POST /` and `GET /health`, register MAX subscription types `message_created`, `message_edited`, `message_callback`, `bot_started`, and relevant lifecycle events, then call `_mark_connected()`.

`_handle_webhook()` must cap request bodies at 1 MiB, constant-time compare `X-Max-Bot-Api-Secret`, validate JSON, deduplicate a bounded 15-minute/10,000-entry fingerprint cache, schedule `_process_update()` with `asyncio.create_task`, attach a done callback that logs failures, and return `200` before agent execution.

`_process_update()` must use `map_update`, reject sender IDs outside `MAX_ALLOWED_USERS`, answer callbacks best-effort, construct Hermes `SessionSource` through `build_source`, construct a real `MessageEvent`, and call `handle_message`.

- [ ] **Step 4: Run listener tests to verify they pass**

Run: `uv run pytest tests/test_adapter.py -k 'webhook' -v`

Expected: PASS.

### Task 5: Add Outbound Text, Replies, Typing, And Per-Chat Pacing

**Files:**
- Modify: `adapter.py`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing outbound tests**

```python
async def test_send_chunks_at_max_limit_and_preserves_reply(adapter, fake_client):
    result = await adapter.send("42", "x" * 4001, reply_to="m0")

    assert result.success is True
    assert [call["text"] for call in fake_client.messages] == ["x" * 4000, "x"]
    assert fake_client.messages[0]["reply_to"] == "m0"


async def test_send_typing_is_noop_for_direct_messages(adapter, fake_client):
    adapter._chat_types["7"] = "dm"

    await adapter.send_typing("7")

    assert fake_client.actions == []
```

- [ ] **Step 2: Run outbound tests to verify they fail**

Run: `uv run pytest tests/test_adapter.py -k 'send_chunks or typing' -v`

Expected: FAIL because `send` and `send_typing` are absent.

- [ ] **Step 3: Implement outbound delivery**

Implement `send(chat_id, content, reply_to=None, metadata=None)` with a per-chat `asyncio.Lock`, safe splitting at paragraphs/newlines/word boundaries below 4,000 characters, and a 0.5-second minimum interval per chat. Use cached inbound chat type to choose `user_id` for DMs and `chat_id` for groups/channels. Convert `metadata.get("max_keyboard")` through `build_keyboard`; reject invalid shapes as a failed `SendResult`.

Implement `send_typing()` only for cached `group` and `channel` types. Implement `get_chat_info()` from bounded in-memory metadata.

- [ ] **Step 4: Run outbound tests to verify they pass**

Run: `uv run pytest tests/test_adapter.py -k 'send_chunks or typing' -v`

Expected: PASS.

### Task 6: Add Native Media Send And Detached Cron Delivery

**Files:**
- Modify: `adapter.py`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing media and cron tests**

```python
async def test_send_image_file_uploads_then_sends_image_attachment(adapter, fake_client, tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"png")
    fake_client.upload_result = {"token": "uploaded"}

    await adapter.send_image_file("42", str(image), caption="image")

    assert fake_client.uploads == [(image, "image")]
    assert fake_client.messages[-1]["attachments"] == [
        {"type": "image", "payload": {"token": "uploaded"}}
    ]


async def test_standalone_sender_uses_configured_home_target_and_reports_id(pconfig, monkeypatch):
    result = await _standalone_send(pconfig, "42", "report")

    assert result == {"success": True, "message_id": "m1"}
```

- [ ] **Step 2: Run media and cron tests to verify they fail**

Run: `uv run pytest tests/test_adapter.py -k 'image_file or standalone_sender' -v`

Expected: FAIL because native media and standalone sender are absent.

- [ ] **Step 3: Implement media and cron sender**

Override `send_image_file`, `send_voice`, `send_video`, and `send_document`. Map files to MAX media types `image`, `audio`, `video`, and `file`, call `MaxClient.upload_file`, then `send_message` with the returned payload. Respect the MAX message attachment combinations: send a document separately from image/video/audio when needed.

Implement `_standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False)` by constructing a temporary `MaxClient`, using the same Markdown text chunking and file-upload functions, closing the client, and returning exactly `{"success": True, "message_id": id}` or `{"error": reason}`. The sender supports `media_files`, unlike LINE, because MAX has a direct upload API.

- [ ] **Step 4: Run media and cron tests to verify they pass**

Run: `uv run pytest tests/test_adapter.py -k 'image_file or standalone_sender' -v`

Expected: PASS.

### Task 7: Register Plugin And Document Deployment

**Files:**
- Modify: `adapter.py`
- Create: `README.md`
- Modify: `docs/max-hermes-plugin-design.md`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write the failing registration test**

```python
def test_register_exposes_max_without_allow_all():
    ctx = RecordingContext()

    register(ctx)

    entry = ctx.platforms["max"]
    assert entry["allowed_users_env"] == "MAX_ALLOWED_USERS"
    assert "allow_all_env" not in entry
    assert entry["cron_deliver_env_var"] == "MAX_HOME_CHANNEL"
    assert entry["max_message_length"] == 4000
```

- [ ] **Step 2: Run registration test to verify it fails**

Run: `uv run pytest tests/test_adapter.py::test_register_exposes_max_without_allow_all -v`

Expected: FAIL because `register` is absent.

- [ ] **Step 3: Implement registration and documentation**

Implement `check_requirements()`, `validate_config(config)`, `_env_enablement()`, `_apply_yaml_config(yaml_cfg, platform_cfg)`, and `register(ctx)`. Register only:

```python
ctx.register_platform(
    name="max",
    label="MAX",
    adapter_factory=lambda cfg: MaxAdapter(cfg),
    check_fn=check_requirements,
    validate_config=validate_config,
    required_env=["MAX_BOT_TOKEN", "MAX_WEBHOOK_URL", "MAX_WEBHOOK_SECRET", "MAX_ALLOWED_USERS"],
    env_enablement_fn=_env_enablement,
    apply_yaml_config_fn=_apply_yaml_config,
    allowed_users_env="MAX_ALLOWED_USERS",
    cron_deliver_env_var="MAX_HOME_CHANNEL",
    standalone_sender_fn=_standalone_send,
    max_message_length=4000,
    pii_safe=False,
    emoji="M",
    platform_hint="You are chatting via MAX. Always use MAX Markdown."
)
```

Do not pass `allow_all_env`.

Document installation into `~/.hermes/plugins/max/`; MAX organization verification and bot moderation; the required variables; local listener at `/`; and the specific deployment path:

```text
MAX -> Cloudflare edge certificate for max-webhook.bimos.noxu.dev
    -> cloudflared tunnel -> kgateway HTTPRoute -> Service -> plugin Pod port 8080
```

State that `MAX_WEBHOOK_URL=https://max-webhook.bimos.noxu.dev/`, Cloudflare owns public TLS, and the plugin owns neither Cloudflare Tunnel nor kgateway resources. Include the token rotation procedure and alert that MAX group/channel access must be enabled in its bot settings.

- [ ] **Step 4: Run registration test to verify it passes**

Run: `uv run pytest tests/test_adapter.py::test_register_exposes_max_without_allow_all -v`

Expected: PASS.

### Task 8: Run Full Verification

**Files:**
- Modify: only files required to correct failures discovered by the commands below.

- [ ] **Step 1: Run all tests**

Run: `uv run pytest -v`

Expected: PASS with no skipped or xfailed tests unless explicitly justified in output.

- [ ] **Step 2: Run static checks**

Run: `uv run ruff check .`

Expected: `All checks passed!`

- [ ] **Step 3: Verify package installation**

Run: `uv build`

Expected: source distribution and wheel are created in `dist/` without errors.

- [ ] **Step 4: Inspect final change set for secrets and scope**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only the plugin, tests, and documentation changes listed in this plan. Do not commit; leave the change set for user review.

## Plan Review

- Design coverage: Tasks 1-7 cover packaging, webhook-only listener, mandatory authorization, secret validation, MAX REST calls, inbound text/callback/media, outbound Markdown/text/replies/typing/media/keyboards, rate limiting, token locks, subscription lifecycle, cron, and operations documentation.
- Deliberate boundary: generic Hermes keyboard/callback APIs do not exist. The plan uses a narrow MAX-specific `metadata["max_keyboard"]` contract and tagged callback messages without modifying Hermes core.
- Scope exclusion: organization verification, bot moderation/configuration, Cloudflare Tunnel, kgateway, TLS, Mini Apps, and channel administration remain documented deployment/operator responsibilities.
