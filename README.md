# Max gateway adapter for Hermes Agent

Webhook-only MAX Messenger gateway plugin for Hermes Agent.

Русская документация: [`docs/README.ru.md`](docs/README.ru.md).

## Prerequisites

- A verified MAX organization and a moderated chatbot with status `created`.
- The bot token from MAX Partner Platform.
- A public HTTPS webhook URL on port 443 with a trusted certificate.
- `MAX_ALLOWED_USERS` containing every MAX user ID permitted to use the bot.

The plugin does not create or moderate bots, provision TLS, or configure Cloudflare Tunnel, kgateway, or Kubernetes resources.

## Installation

Install the plugin from GitHub:

```bash
hermes plugins install https://github.com/Deezzir/max-platform
```

For a named profile, run the command with `-p <profile>`. The plugin runs inside Hermes and uses Hermes's installed Python runtime.

Enable it in the profile `config.yaml`:

```yaml
gateway:
  platforms:
    max:
      enabled: true
```

Put secrets in the profile `.env`, not in `config.yaml`:

```env
MAX_BOT_TOKEN=...
MAX_WEBHOOK_URL=https://your-public-domain.com/
MAX_WEBHOOK_SECRET=...
MAX_ALLOWED_USERS=123456789
```

Optional `.env` settings are `MAX_LISTEN_HOST` (default `0.0.0.0`), `MAX_LISTEN_PORT` (default `8080`), and `MAX_HOME_CHANNEL` for cron delivery. `MAX_REQUIRE_MENTION=true` requires an `@bot_username` mention in groups and channels.

The plugin listens at local `POST /` and exposes `GET /health`. It always sends MAX Markdown and never supports open access.

## Deployment

The plugin listens on its configured local port. Deployment infrastructure must forward the public HTTPS callback URL to that port. MAX validates the public HTTPS endpoint and its trusted certificate.

Enable group-chat access in MAX bot settings before using group chats or channels.

## Token Rotation

Rotate the token in MAX Partner Platform, update `MAX_BOT_TOKEN`, restart Hermes, then verify the gateway connects and receives a webhook. A MAX `401` is treated as a configuration failure and is not retried.
