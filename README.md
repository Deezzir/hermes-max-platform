# MAX Messenger plugin for Hermes Agent

Это плагин MAX Мессенджер для Hermes Agent с официальным Bot API, webhook и ограничением доступа. Полная инструкция: [`docs/README.ru.md`](docs/README.ru.md).

Webhook-only MAX Messenger plugin for Hermes Agent. It connects an official MAX chatbot to Hermes Gateway through the MAX Bot API.

## Prerequisites

- A verified MAX organization and a moderated chatbot with status `created`.
- The bot token from MAX Partner Platform.
- A public HTTPS webhook URL on port 443 with a trusted certificate.
- `MAX_ALLOWED_USERS` containing every MAX user ID permitted to use the bot.

The host must trust the Russian root certificate used by `https://platform-api2.max.ru`. Download it before starting Hermes:

```bash
curl --fail --location --output russian-trusted-root-ca.crt \
  https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
```

Install the certificate in the operating system or Python trust store used by Hermes. Do not place the certificate in this repository or commit it.

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

### Nginx Reverse Proxy

MAX must reach a public HTTPS URL. Configure Nginx to terminate TLS and forward requests to the plugin's local listener:

```nginx
server {
    listen 443 ssl http2;
    server_name bot.example.com;

    ssl_certificate /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;

    client_max_body_size 1m;

    location = / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /health {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
    }
}
```

For this configuration, set the profile `.env` values:

```env
MAX_WEBHOOK_URL=https://bot.example.com/
MAX_LISTEN_HOST=127.0.0.1
MAX_LISTEN_PORT=8080
```

Nginx forwards `X-Max-Bot-Api-Secret` without extra configuration. Do not remove or replace that header; the plugin verifies it on every webhook.

## Token Rotation

Rotate the token in MAX Partner Platform, update `MAX_BOT_TOKEN`, restart Hermes, then verify the gateway connects and receives a webhook. A MAX `401` is treated as a configuration failure and is not retried.
