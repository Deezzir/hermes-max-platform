# MAX Hermes Plugin Design

## Purpose

This directory plugin connects a Hermes gateway profile to a MAX bot. It runs an aiohttp webhook listener, receives MAX updates, converts them into Hermes message events, and sends Hermes responses through the MAX Bot API.

## Layout

```text
max-platform/
  __init__.py
  plugin.yaml
  src/
    adapter.py
    event_mapper.py
    max_client.py
```

The root entry point exports `register`. `adapter.py` owns the Hermes platform adapter, `event_mapper.py` converts MAX updates, and `max_client.py` owns HTTP calls to `platform-api2.max.ru`.

## Configuration

Required variables are `MAX_BOT_TOKEN`, `MAX_WEBHOOK_URL`, `MAX_WEBHOOK_SECRET`, and `MAX_ALLOWED_USERS`.

Optional variables are `MAX_LISTEN_HOST` (default `0.0.0.0`), `MAX_LISTEN_PORT` (default `8080`), `MAX_HOME_CHANNEL`, and `MAX_REQUIRE_MENTION`.

`MAX_REQUIRE_MENTION` applies only to group chats and channels. Direct messages always reach Hermes. `MAX_ALLOWED_USERS` applies to messages and callback-button clicks.

## Runtime

On connection the adapter validates its token with `GET /me`, starts the local listener, registers a webhook subscription for message, edit, callback, and bot-start updates, and registers up to 32 MAX bot-menu commands.

The listener accepts `POST /`, validates `X-Max-Bot-Api-Secret`, deduplicates recent full update payloads in memory, acknowledges the request, and processes the update in a background task.

Direct-message replies use MAX `user_id`. Group and channel replies use `chat_id`. Group recipient IDs are preserved when MAX supplies a negative recipient chat ID without a recipient type.

## Sending And Interaction

Outbound text uses MAX Markdown and is split at 4,000 characters. The adapter supports files, typing actions in groups/channels, inline keyboards supplied as `max_keyboard` metadata, and MAX callback acknowledgements.

The adapter also implements Hermes interactive hooks for clarification choices, command approvals, slash confirmations, update prompts, finite choice pickers, and model selection. Interactive state is bounded to 1,000 pending controls. Model navigation edits the existing picker message when MAX returns its message ID.

MAX supports up to 30 keyboard rows and 7 buttons per row. Generic pickers exceeding 30 rows return a failure so Hermes can use its text fallback.

## Limits

The client starts requests no faster than 30 per second and retries MAX `429` and `503` responses. It does not implement token locking, subscription deletion, attachment-readiness retries, or native draft streaming. MAX has no native draft API, so Hermes uses normal message delivery for streaming fallbacks.

## Verification

Run `tox`. It checks formatting, linting, typing, and the test suite.
