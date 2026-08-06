# MAX Hermes Plugin Design

## Goal

Deliver a standalone Hermes platform plugin that lets users converse with Hermes through a MAX chatbot. The plugin runs a local HTTP webhook listener and uses MAX webhooks exclusively. Deployment infrastructure is responsible for exposing and forwarding the public HTTPS callback to that listener.

## Scope

The plugin supports:

- Inbound direct messages, group-chat messages, channel posts, edits, replies, forwarded content, media, contacts, locations, stickers, shares, bot-start events, and inline-button callbacks.
- Outbound Markdown text, safe 4,000-character chunking, replies, group-chat typing indicators, media uploads, and MAX inline keyboards.
- Hermes platform registration, authorization environment variables, YAML configuration bridging, platform guidance, cron home-channel delivery, and out-of-process cron sends.
- Operational safeguards: webhook-secret validation, duplicate-event suppression, MAX rate limiting, per-chat send pacing, retry handling, token locking, and redacted structured logging.

The plugin does not create, moderate, configure, or delete MAX bots; create channels; administer chats or channels; or implement a MAX Mini App. Those are managed through MAX's Partner Platform and messenger UI.

## Preconditions

Before installing the plugin, an operator must:

1. Create and verify an eligible MAX organization profile and create the chatbot through MAX Partner Platform.
2. Wait until the bot has completed moderation and its status is `created`.
3. Retrieve the bot token from the MAX integration settings.
4. Enable group-chat access in the bot's advanced settings when group chats or channels are required. MAX disables this by default, and disabling it prevents both group and channel use.
5. Publish an HTTPS webhook URL on port 443 using a certificate from a trusted CA and forward its configured callback path to the plugin's local listener.

The installation guide records MAX's organization, bot-metadata, and moderation constraints, but the plugin neither collects nor stores organization verification documents.

## Plugin Layout

The distributable plugin has a small, focused layout:

```text
max-hermes-plugin/
  plugin.yaml
  __init__.py
  src/
    __init__.py
    adapter.py
    max_client.py
    event_mapper.py
  tests/
  docs/
```

Users install the plugin by placing or linking this directory at `~/.hermes/plugins/max/`. The root `__init__.py` provides Hermes's `register(ctx)` entry point; `src/adapter.py` provides `MaxAdapter`, while the other modules isolate MAX REST access and event conversion.

## Configuration

Required environment variables:

| Variable | Purpose |
| --- | --- |
| `MAX_BOT_TOKEN` | MAX bot API token; sent only in the `Authorization` header. |
| `MAX_WEBHOOK_URL` | Public HTTPS URL registered with MAX. |
| `MAX_WEBHOOK_SECRET` | Shared secret sent and verified through `X-Max-Bot-Api-Secret`. |

Optional environment variables:

| Variable | Purpose |
| --- | --- |
| `MAX_LISTEN_HOST` | Local bind host; defaults to the loopback address. |
| `MAX_LISTEN_PORT` | Local listener port to which deployment infrastructure forwards the public callback. |
| `MAX_HOME_CHANNEL` | Default chat/channel ID for cron delivery. |
| `MAX_ALLOWED_USERS` | Comma-separated MAX user IDs authorized to use Hermes. |
| `MAX_REQUIRE_MENTION` | Require an explicit mention in group chats. |

`plugin.yaml` surfaces these settings in `hermes config`. The YAML bridge maps MAX-specific keys into equivalent settings while preserving environment-variable precedence.

`MAX_ALLOWED_USERS` is mandatory; the plugin does not provide an allow-all mode. All outbound text uses MAX Markdown and does not expose a format setting.

## Connection Lifecycle

`connect()` performs the following sequence:

1. Read and validate the required configuration.
2. Acquire Hermes's scoped token lock for the MAX bot token.
3. Create one reusable asynchronous HTTP client for `https://platform-api2.max.ru`.
4. Call `GET /me`; fail connection on `401` or invalid bot identity.
5. Start the internal aiohttp listener.
6. Reconcile the configured MAX webhook subscription by calling `POST /subscriptions` with the public URL, webhook secret, and supported event types.
7. Mark the Hermes adapter connected only after the listener and subscription are ready.

`disconnect()` stops accepting new webhook requests, waits briefly for accepted event tasks, cancels remaining tasks, closes the listener and client, removes only the subscription matching the configured public URL, and releases the scoped token lock.

The plugin never uses MAX long polling. Production webhook delivery requires the public endpoint to be reachable on HTTPS port 443 with a trusted full certificate chain.

## Inbound Delivery

The local listener accepts webhook POST requests at `/`.

1. Compare `X-Max-Bot-Api-Secret` against `MAX_WEBHOOK_SECRET` using constant-time comparison. Return an authorization error when it is absent or invalid.
2. Validate JSON shape and derive a stable event fingerprint from the MAX update type, timestamp, chat ID, message/callback ID, and sender ID.
3. Suppress recently seen fingerprints using a bounded TTL cache because MAX retries failed delivery.
4. Return `200 OK` promptly for an accepted update, then process it in a supervised background task. This keeps delivery within MAX's 30-second acknowledgement deadline.
5. Log only redacted identifiers and processing outcomes. Do not log token values, webhook secrets, user contact data, or media URLs containing credentials.

Events handled by the adapter:

| MAX event | Hermes behavior |
| --- | --- |
| `message_created` | Convert to a `MessageEvent` and call `handle_message`. |
| `message_edited` | Convert to a message event with edit metadata. |
| `message_callback` | Convert callback data to a supported Hermes callback event, or a clearly tagged text event when no callback event type exists; acknowledge through `POST /answers`. |
| `bot_started` | Send a normal start-context message event, including the optional deep-link payload. |
| Membership, dialog, title, and removal events | Update bounded in-memory chat metadata only; do not invoke the agent. |

The mapper retains sender ID/name, chat ID/name/type, message ID, reply and forward linkage, timestamp, supported attachments, and raw platform metadata necessary for response routing. Contact attachments are redacted from agent-visible context by default. Enabling contact exposure requires an explicit future policy setting.

## Outbound Delivery

`send(chat_id, content, reply_to, metadata)` uses `POST /messages` with `chat_id` for group chats/channels or `user_id` for direct messages. It:

1. Renders text as MAX Markdown by default.
2. Splits content safely below MAX's 4,000-character message limit.
3. Preserves reply links when `reply_to` has a MAX message ID.
4. Converts supported Hermes metadata to MAX inline keyboards, including callback, link, message, clipboard, contact, geolocation, and Mini App buttons.
5. Serializes sends per conversation and spaces them to obey MAX's two-messages-per-second limit.
6. Returns a Hermes `SendResult` using the MAX message ID.

`send_typing()` calls `POST /chats/{chatId}/actions` with `typing_on` for group chats and channels. It is intentionally a no-op for direct messages because MAX documents the action endpoint for chats only.

`get_chat_info()` returns cached chat metadata and may query MAX chat details when that endpoint is applicable.

## Command Menu And Interactive Controls

On connection, the adapter synchronizes MAX's bot command menu through `PATCH /me/commands`. MAX permits at most 32 menu entries, so the plugin registers the highest-priority gateway commands using Hermes's canonical command-registry ordering. Every gateway command remains supported when typed, and `/commands` remains the entry point for commands that do not fit in MAX's native menu.

The adapter implements Hermes's optional interactive platform hooks using MAX callback buttons:

- `send_clarify()` renders a button for each choice and an `Other` button that returns the user to Hermes's text-input fallback.
- `send_slash_confirm()` renders Approve Once, Always Approve, and Cancel for destructive and expensive slash commands, including `/new`, `/clear`, `/reset`, `/undo`, `/model`, and `/reload-mcp` confirmations.
- `send_choice_picker()` renders finite selectors used by `/reasoning`, `/fast`, and future compatible gateway commands.
- `send_model_picker()` renders a paginated provider selector followed by a paginated model selector for `/model`.

The adapter keeps short-lived picker state keyed by chat and callback identifier. It authorizes the callback sender before invoking the Hermes callback or resolver supplied by the hook. Expired, invalid, unauthorized, or failed interactive sends fall back to the existing typed-command or text-response flow. After a choice resolves, the adapter updates the picker message through `PUT /messages/{messageId}` to show the result without active buttons when MAX permits the edit.

## Media

Inbound attachments retain their MAX type and metadata for Hermes media processing: image, video, audio, file, sticker, contact, location, share, inline keyboard, and linked/replied content.

For outbound files, the adapter selects `image`, `video`, `audio`, or `file`, then:

1. Requests an upload URL through `POST /uploads?type=<type>`.
2. Uploads the file using multipart form field `data`.
3. Builds a MAX attachment using the returned token/payload.
4. Sends the message through `POST /messages`.

When MAX returns `attachment.not.ready`, the adapter retries the message with bounded exponential backoff. It does not retry unsupported file types, validation errors, or authentication errors. The documentation states MAX's current size/format constraints and notes that image URLs can be sent directly when Hermes has a suitable public URL.

## Reliability And Security

- A shared limiter caps API calls below MAX's 30 RPS limit.
- Per-chat queues prevent violating MAX's two outbound messages per second constraint.
- Retry only retryable `429`, `503`, transient network failures, and `attachment.not.ready` responses; honor server retry hints where available.
- Treat `400`, `401`, `404`, and unsupported attachment errors as terminal, actionable failures.
- Keep deduplication and chat metadata bounded by size and TTL.
- Use the `Authorization` request header only; never add the token to query parameters.
- Require a webhook secret and reject unauthenticated delivery.
- Include a `standalone_sender_fn` so cron delivery works outside the gateway process.

## Token Rotation

MAX token rotation is an operator procedure:

1. Rotate the token in MAX Partner Platform.
2. Replace `MAX_BOT_TOKEN` in the deployment secret.
3. Restart or reload Hermes so the adapter creates a new HTTP client and subscription session.
4. Verify `GET /me`, gateway status, and the active MAX webhook subscription.

An API `401` is logged as a non-retryable configuration failure rather than retried. The adapter remains disconnected until valid credentials are supplied.

## Hermes Registration

`register(ctx)` registers the platform as `max` with:

- A `MaxAdapter` factory.
- Requirement and configuration validators.
- Environment-driven auto-enablement and optional YAML bridge.
- User authorization variables.
- `MAX_HOME_CHANNEL` cron delivery and a standalone sender.
- A 4,000-character platform limit for Hermes smart chunking.
- A MAX Markdown platform hint.
- Plugin-provided configuration UI metadata and an install hint for dependencies.

## Testing

Tests use a fake MAX API and local aiohttp listener. They cover:

- Required configuration, environment enablement, YAML precedence, and registration metadata.
- Startup identity validation, token locks, subscription reconciliation, and clean shutdown.
- Webhook path/method validation, secret validation, duplicate suppression, prompt acknowledgement, and task failure isolation.
- Mapping each supported inbound update and attachment, including redaction of contacts.
- Text chunking, Markdown requests, replies, callbacks, keyboards, typing behavior, and direct-message routing.
- Upload/token handling, media payloads, `attachment.not.ready` retries, and terminal media failures.
- Global API rate limiting, per-chat pacing, retry classification, and cron standalone delivery.

## Documentation And Operations

The project documentation includes:

- Installation and plugin placement.
- MAX organization verification, bot creation, moderation, and token acquisition prerequisites.
- The deployment requirement to expose the callback path on public HTTPS port 443 and forward it to the local listener. Reverse-proxy and tunnel configuration are deployment-owned and not implemented by the plugin.
- Required/optional configuration variables and authorization guidance.
- Group and channel enablement in MAX's advanced bot settings.
- Health checks, token rotation, webhook-secret rotation, and recovery from MAX webhook retries or automatic unsubscription after extended endpoint failures.
- MAX bot metadata requirements and the statement that Mini Apps and channel administration are separate products outside this plugin.

## Acceptance Criteria

The implementation is complete when an operator can install the plugin, configure a moderated MAX bot and trusted public HTTPS callback that forwards to the listener, start Hermes, and reliably exchange text, callbacks, supported media, and supported interactive controls with the agent through MAX. The adapter must respect MAX webhook, authentication, command-menu, message-size, per-chat, and global rate limits; keep secrets and contact data out of logs; and support both live gateway and out-of-process cron delivery.
