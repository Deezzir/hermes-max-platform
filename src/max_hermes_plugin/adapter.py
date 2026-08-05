from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from aiohttp import web
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

from .event_mapper import build_keyboard, event_fingerprint, map_update
from .max_client import MaxClient


logger = logging.getLogger(__name__)
_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


class MaxAdapter(BasePlatformAdapter):
    def __init__(self, config: Any) -> None:
        super().__init__(config, Platform("max"))
        extra = getattr(config, "extra", {}) or {}
        self._token = os.getenv("MAX_BOT_TOKEN") or extra.get("token", "")
        self._webhook_url = os.getenv("MAX_WEBHOOK_URL") or extra.get("webhook_url", "")
        self._webhook_secret = os.getenv("MAX_WEBHOOK_SECRET") or extra.get("webhook_secret", "")
        allowed = os.getenv("MAX_ALLOWED_USERS") or extra.get("allowed_users", "")
        self._allowed_users = {part.strip() for part in str(allowed).split(",") if part.strip()}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._client: MaxClient | Any | None = None
        self._chat_types: OrderedDict[str, str] = OrderedDict()
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._last_send: dict[str, float] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not all((self._token, self._webhook_url, self._webhook_secret, self._allowed_users)):
            self._set_fatal_error(
                "config_missing", "MAX configuration is incomplete", retryable=False
            )
            return False
        self._client = MaxClient(self._token)
        try:
            await self._client.get_me()
            self._runner = web.AppRunner(self.create_app())
            await self._runner.setup()
            host = os.getenv("MAX_LISTEN_HOST", "0.0.0.0")
            port = int(os.getenv("MAX_LISTEN_PORT", "8080"))
            self._site = web.TCPSite(self._runner, host, port)
            await self._site.start()
            await self._client.register_subscription(
                self._webhook_url,
                self._webhook_secret,
                ["message_created", "message_edited", "message_callback", "bot_started"],
            )
        except Exception as exc:
            await self.disconnect()
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._mark_disconnected()

    def create_app(self) -> web.Application:
        app = web.Application(client_max_size=_MAX_WEBHOOK_BODY_BYTES)
        app.router.add_post("/", self._handle_webhook)
        app.router.add_get("/health", self._handle_health)
        return app

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "platform": "max"})

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        supplied = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not self._webhook_secret or not hmac.compare_digest(supplied, self._webhook_secret):
            return web.Response(status=401, text="invalid webhook secret")
        try:
            update = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json")
        if not isinstance(update, dict):
            return web.Response(status=400, text="invalid update")
        fingerprint = event_fingerprint(update)
        if fingerprint in self._seen:
            return web.Response(status=200)
        self._seen[fingerprint] = None
        if len(self._seen) > 10_000:
            self._seen.popitem(last=False)
        task = asyncio.create_task(self._process_update(update))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_task_failure)
        return web.Response(status=200)

    @staticmethod
    def _log_task_failure(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("MAX webhook processing failed")

    async def _process_update(self, update: dict[str, Any]) -> None:
        mapped = map_update(update)
        if mapped is None or mapped.user_id not in self._allowed_users:
            return
        source = self.build_source(
            chat_id=mapped.chat_id,
            chat_name=mapped.chat_id,
            chat_type=mapped.chat_type,
            user_id=mapped.user_id,
            user_name=mapped.user_name,
        )
        self._chat_types[mapped.chat_id] = mapped.chat_type
        if len(self._chat_types) > 10_000:
            self._chat_types.popitem(last=False)
        event = MessageEvent(
            text=mapped.text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=mapped.message_id,
            raw_message=mapped.raw_update,
            metadata={"edited": mapped.edited},
        )
        callback = update.get("callback") or update.get("message_callback") or {}
        callback_id = callback.get("callback_id") or callback.get("id")
        if (
            update.get("update_type") == "message_callback"
            and callback_id
            and self._client is not None
        ):
            try:
                await self._client.answer_callback(str(callback_id))
            except RuntimeError:
                logger.warning("MAX callback acknowledgement failed")
        await self.handle_message(event)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="MAX adapter is not connected")
        attachments = None
        try:
            if metadata and metadata.get("max_keyboard") is not None:
                attachments = [build_keyboard(metadata["max_keyboard"])]
        except ValueError as exc:
            return SendResult(success=False, error=str(exc))
        lock = self._send_locks.setdefault(chat_id, asyncio.Lock())
        message_id = None
        async with lock:
            for chunk in self._split_text(content):
                delay = 0.5 - (time.monotonic() - self._last_send.get(chat_id, 0.0))
                if delay > 0:
                    await asyncio.sleep(delay)
                kwargs: dict[str, Any] = {
                    "text": chunk,
                    "attachments": attachments,
                    "reply_to": reply_to,
                }
                if self._chat_types.get(chat_id) == "dm":
                    kwargs["user_id"] = chat_id
                else:
                    kwargs["chat_id"] = chat_id
                try:
                    response = await self._client.send_message(**kwargs)
                except RuntimeError as exc:
                    return SendResult(success=False, error=str(exc))
                self._last_send[chat_id] = time.monotonic()
                message_id = response.get("message_id")
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        if self._client is not None and self._chat_types.get(chat_id) in {"group", "channel"}:
            await self._client.send_action(chat_id)

    async def get_chat_info(self, chat_id: str) -> dict[str, str]:
        return {"name": chat_id, "type": self._chat_types.get(chat_id, "dm")}

    async def send_image_file(
        self, chat_id: str, image_path: str, caption: str = "", **kwargs: Any
    ) -> SendResult:
        return await self._send_file(
            chat_id, Path(image_path), "image", caption, kwargs.get("reply_to")
        )

    async def send_voice(
        self, chat_id: str, audio_path: str, caption: str = "", **kwargs: Any
    ) -> SendResult:
        return await self._send_file(
            chat_id, Path(audio_path), "audio", caption, kwargs.get("reply_to")
        )

    async def send_video(
        self, chat_id: str, video_path: str, caption: str = "", **kwargs: Any
    ) -> SendResult:
        return await self._send_file(
            chat_id, Path(video_path), "video", caption, kwargs.get("reply_to")
        )

    async def send_document(
        self, chat_id: str, file_path: str, caption: str = "", **kwargs: Any
    ) -> SendResult:
        return await self._send_file(
            chat_id, Path(file_path), "file", caption, kwargs.get("reply_to")
        )

    async def _send_file(
        self, chat_id: str, path: Path, media_type: str, caption: str, reply_to: str | None
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="MAX adapter is not connected")
        try:
            payload = await self._client.upload_file(path, media_type)
            kwargs: dict[str, Any] = {
                "text": caption or None,
                "attachments": [{"type": media_type, "payload": payload}],
                "reply_to": reply_to,
            }
            if self._chat_types.get(chat_id) == "dm":
                kwargs["user_id"] = chat_id
            else:
                kwargs["chat_id"] = chat_id
            response = await self._client.send_message(**kwargs)
        except (OSError, ValueError, RuntimeError) as exc:
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=response.get("message_id"))

    @staticmethod
    def _split_text(content: str) -> list[str]:
        if not content:
            return [""]
        chunks = []
        remaining = content
        while len(remaining) > 4000:
            cut = max(remaining.rfind("\n", 0, 4000), remaining.rfind(" ", 0, 4000))
            cut = cut if cut > 2000 else 4000
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        chunks.append(remaining)
        return chunks


def check_requirements() -> bool:
    return True


def validate_config(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return (
        bool(os.getenv("MAX_BOT_TOKEN") or extra.get("token"))
        and bool(os.getenv("MAX_WEBHOOK_URL") or extra.get("webhook_url"))
        and bool(os.getenv("MAX_WEBHOOK_SECRET") or extra.get("webhook_secret"))
        and bool(os.getenv("MAX_ALLOWED_USERS") or extra.get("allowed_users"))
    )


def _env_enablement() -> dict[str, Any] | None:
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    webhook_url = os.getenv("MAX_WEBHOOK_URL", "").strip()
    webhook_secret = os.getenv("MAX_WEBHOOK_SECRET", "").strip()
    allowed_users = os.getenv("MAX_ALLOWED_USERS", "").strip()
    if not all((token, webhook_url, webhook_secret, allowed_users)):
        return None
    result: dict[str, Any] = {
        "token": token,
        "webhook_url": webhook_url,
        "webhook_secret": webhook_secret,
        "allowed_users": allowed_users,
    }
    home = os.getenv("MAX_HOME_CHANNEL", "").strip()
    if home:
        result["home_channel"] = {"chat_id": home, "name": "MAX Home"}
    return result


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    extra = getattr(pconfig, "extra", {}) or {}
    token = os.getenv("MAX_BOT_TOKEN") or extra.get("token", "")
    if not token:
        return {"error": "MAX_BOT_TOKEN is not configured"}
    client = MaxClient(token)
    try:
        attachments = []
        for file_name in media_files or []:
            path = Path(file_name)
            media_type = "file" if force_document else _media_type(path)
            payload = await client.upload_file(path, media_type)
            attachments.append({"type": media_type, "payload": payload})
        result = await client.send_message(
            chat_id=chat_id, text=message, attachments=attachments or None
        )
        return {"success": True, "message_id": result.get("message_id")}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"error": str(exc)}
    finally:
        await client.close()


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".tiff", ".bmp", ".heic"}:
        return "image"
    if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
        return "video"
    if suffix in {".mp3", ".wav", ".m4a", ".ogg"}:
        return "audio"
    return "file"


def register(ctx: Any) -> None:
    ctx.register_platform(
        name="max",
        label="MAX",
        adapter_factory=lambda config: MaxAdapter(config),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[
            "MAX_BOT_TOKEN",
            "MAX_WEBHOOK_URL",
            "MAX_WEBHOOK_SECRET",
            "MAX_ALLOWED_USERS",
        ],
        allowed_users_env="MAX_ALLOWED_USERS",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MAX_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=4000,
        pii_safe=False,
        emoji="M",
        platform_hint="You are chatting via MAX. Always use MAX Markdown.",
    )
