# Max gateway adapter for Hermes Agent

Webhook-only MAX Messenger gateway plugin for Hermes Agent.

## Prerequisites

- A verified MAX organization and a moderated chatbot with status `created`.
- The bot token from MAX Partner Platform.
- A public HTTPS webhook URL on port 443 with a trusted certificate.
- `MAX_ALLOWED_USERS` containing every MAX user ID permitted to use the bot.

The plugin does not create or moderate bots, provision TLS, or configure Cloudflare Tunnel, kgateway, or Kubernetes resources.

## Installation

Place this repository at `~/.hermes/plugins/max/`. Hermes must be installed with its messaging runtime, which supplies `aiohttp`; Tenacity is already a Hermes core dependency. No separate plugin dependency installation is required.

Set the required secrets in the Hermes environment:

```env
MAX_BOT_TOKEN=...
MAX_WEBHOOK_URL=https://max-webhook.bimos.noxu.dev/
MAX_WEBHOOK_SECRET=...
MAX_ALLOWED_USERS=123456789
```

Optional settings are `MAX_LISTEN_HOST` (default `0.0.0.0`), `MAX_LISTEN_PORT` (default `8080`), and `MAX_HOME_CHANNEL` for cron delivery.

The plugin listens at local `POST /` and exposes `GET /health`. It always sends MAX Markdown and never supports open access.

## Kubernetes Deployment

The plugin listens on its Pod port. Deployment infrastructure forwards the public URL to that port:

```text
MAX -> Cloudflare edge certificate for max-webhook.bimos.noxu.dev
    -> cloudflared tunnel -> kgateway HTTPRoute -> Service -> plugin Pod:8080
```

Cloudflare owns the public TLS certificate. The hostname must be covered by the edge certificate, and MAX validates the public HTTPS endpoint, not the in-cluster service.

Enable group-chat access in MAX bot settings before using group chats or channels.

## Token Rotation

Rotate the token in MAX Partner Platform, update `MAX_BOT_TOKEN`, restart Hermes, then verify the gateway connects and receives a webhook. A MAX `401` is treated as a configuration failure and is not retried.
