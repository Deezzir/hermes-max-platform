# MAX Messenger Plugin for Hermes Agent

> [!NOTE]
> Это плагин MAX Мессенджер для Hermes Agent с официальным Bot API, webhook и ограничением доступа. Полная инструкция: [`docs/README.ru.md`](docs/README.ru.md).

The official MAX Messenger webhook plugin for Hermes Agent connects a Hermes Gateway profile to a MAX chatbot. It receives events through the MAX Bot API and sends Hermes responses back to MAX.

## Supported Features

- Direct messages, group chats, and channels.
- Markdown text, files, and typing indicators in groups and channels.
- Buttons for model selection, confirmations, clarification questions, and other supported Hermes interactive flows.
- MAX bot commands: the native menu holds up to 32 commands; remaining commands are available through `/commands` and manual input.
- Cron delivery to `MAX_HOME_CHANNEL`.

## Prerequisites

- A verified organization, sole proprietor, or self-employed profile on the MAX platform.
- A moderated MAX chatbot with status `created`.
- A bot token from MAX Partner Platform.
- A public HTTPS URL with a trusted certificate that MAX can reach.
- Allowed user IDs in `MAX_ALLOWED_USERS`.

### Root Certificate for MAX API

The plugin connects to `https://platform-api2.max.ru`. The Hermes host must trust the Russian root certificate. Download the certificate:

```bash
curl --fail --location --output russian-trusted-root-ca.crt \
  https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
```

Install it in the operating system or Python trust store used by Hermes. Do not add the certificate to this repository or commit it.

The plugin does not create or moderate MAX bots, issue TLS certificates, or configure tunnels, reverse proxies, or Kubernetes.

## Installation

Install the plugin from GitHub:

```bash
hermes plugins install https://github.com/Deezzir/max-platform
```

For a named profile, add `-p <profile>`. The plugin runs in the Python environment installed with Hermes.

Enable the platform in the profile `config.yaml`:

```yaml
gateway:
  platforms:
    max:
      enabled: true
```

Set secrets in the profile `.env`, not in `config.yaml`:

```env
MAX_BOT_TOKEN=...
MAX_WEBHOOK_URL=https://bot.example.com/
MAX_WEBHOOK_SECRET=...
MAX_ALLOWED_USERS=123456789,987654321
```

Required variables:

| Variable | Purpose |
| --- | --- |
| `MAX_BOT_TOKEN` | MAX Bot API token. |
| `MAX_WEBHOOK_URL` | Public HTTPS URL registered with MAX. |
| `MAX_WEBHOOK_SECRET` | Secret for the `X-Max-Bot-Api-Secret` header. |
| `MAX_ALLOWED_USERS` | Comma-separated allowed MAX user IDs. |

Optional variables:

| Variable | Purpose |
| --- | --- |
| `MAX_LISTEN_HOST` | Local listener address; defaults to `0.0.0.0`. |
| `MAX_LISTEN_PORT` | Local listener port; defaults to `8080`. |
| `MAX_HOME_CHANNEL` | Chat or channel ID for cron delivery. |
| `MAX_REQUIRE_MENTION` | When `true`, the bot responds in groups and channels only to messages mentioning `@bot_username`. It does not affect direct messages. |

## Webhook and Deployment

The plugin listens on:

- `POST /` for MAX webhooks.
- `GET /health` for health checks.

Infrastructure must forward the public HTTPS URL to the plugin's local port:

```text
MAX -> public HTTPS -> reverse proxy/tunnel -> service -> Hermes:MAX_LISTEN_PORT
```

Enable group-chat access in MAX bot settings before using the bot in groups or channels.

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

For this configuration, add to the profile `.env`:

```env
MAX_WEBHOOK_URL=https://bot.example.com/
MAX_LISTEN_HOST=127.0.0.1
MAX_LISTEN_PORT=8080
```

Nginx forwards `X-Max-Bot-Api-Secret` without extra configuration. Do not remove or replace this header: the plugin verifies it for every webhook.

## Groups and Mentions

In a group, the bot replies to the same group chat rather than to the user's direct messages.

With `MAX_REQUIRE_MENTION=true`, use:

```text
@bot_username /model
@bot_username help me with a task
```

The bot mention is removed before Hermes handles the command. Therefore, `@bot_username /model` invokes the normal `/model` command.

## Buttons and Commands

MAX supports inline buttons. The plugin uses them for model selection, option pickers, clarification questions, and confirmations. Buttons are available only to users in `MAX_ALLOWED_USERS`.

The model picker shows up to 8 models per page with `Previous`, `Next`, `Back`, and `Close` buttons. `Close` cancels the selection and removes the buttons.

MAX can register up to 32 commands in the bot menu. When typing `/`, MAX displays available commands; they can also be selected from the bot menu, entered manually, or viewed with `/commands`.

## Verification

After starting Hermes, check:

```bash
curl http://127.0.0.1:8080/health
```

Expected response:

```json
{"status": "ok", "platform": "max"}
```

Check Hermes logs for listener startup, webhook registration, and MAX adapter connection messages.

## Token Rotation

1. Issue a new token in MAX Partner Platform.
2. Update `MAX_BOT_TOKEN` in the Hermes profile environment.
3. Restart Gateway.
4. Verify that the webhook and `/health` endpoint work.

A MAX `401` indicates a token configuration issue and is not retried automatically.

## Development

Run checks with:

```bash
tox
```

Do not add tokens, webhook secrets, personal user data, or deployment configuration to the repository.
