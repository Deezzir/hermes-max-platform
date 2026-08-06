from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret
from aiohttp import web
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

from .event_mapper import build_keyboard, event_fingerprint, map_update
from .max_client import MaxClient

logger = logging.getLogger(__name__)
_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
_MAX_COMMAND_LIMIT = 32
_MAX_INTERACTION_LIMIT = 1_000


def _get_scoped_secret(name: str, default: str = "") -> str:
    try:
        value = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        value = os.getenv(name, default)
    return value if value is not None else default


class MaxAdapter(BasePlatformAdapter):
    def __init__(self, config: Any) -> None:
        super().__init__(config, Platform("max"))
        extra = getattr(config, "extra", {}) or {}
        self._token = _get_scoped_secret("MAX_BOT_TOKEN") or extra.get("token", "")
        self._webhook_url = _get_scoped_secret("MAX_WEBHOOK_URL") or extra.get("webhook_url", "")
        self._webhook_secret = _get_scoped_secret("MAX_WEBHOOK_SECRET") or extra.get(
            "webhook_secret", ""
        )
        allowed = _get_scoped_secret("MAX_ALLOWED_USERS") or extra.get("allowed_users", "")
        require_mention = _get_scoped_secret("MAX_REQUIRE_MENTION") or extra.get(
            "require_mention", ""
        )
        self._allowed_users = {part.strip() for part in str(allowed).split(",") if part.strip()}
        self._require_mention = str(require_mention).lower() in {"1", "true", "yes", "on"}
        self._bot_username = ""
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._client: MaxClient | Any | None = None
        self._chat_types: OrderedDict[str, str] = OrderedDict()
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._last_send: dict[str, float] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._status_messages: dict[tuple[str, str], str] = {}
        self._interactive: OrderedDict[
            str, tuple[str, str | None, Callable[[str], Awaitable[str | None]]]
        ] = OrderedDict()
        self._interactive_counter = 0

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not all((self._token, self._webhook_url, self._webhook_secret, self._allowed_users)):
            self._set_fatal_error(
                "config_missing", "MAX configuration is incomplete", retryable=False
            )
            return False
        self._client = MaxClient(self._token)
        try:
            bot = await self._client.get_me()
            self._bot_username = str(bot.get("username") or "")
            self._runner = web.AppRunner(self.create_app())
            await self._runner.setup()
            host = os.getenv("MAX_LISTEN_HOST", "0.0.0.0")
            port = int(os.getenv("MAX_LISTEN_PORT", "8080"))
            self._site = web.TCPSite(self._runner, host, port)
            await self._site.start()
            logger.info("MAX webhook listener started host=%s port=%s", host, port)
            await self._client.register_subscription(
                self._webhook_url,
                self._webhook_secret,
                ["message_created", "message_edited", "message_callback", "bot_started"],
            )
            logger.info("MAX webhook subscription registered url=%s", self._webhook_url)
            await self._register_commands()
        except Exception as exc:
            logger.exception("MAX connection failed")
            await self.disconnect()
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False
        self._mark_connected()
        logger.info("MAX adapter connected")
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
        logger.info("MAX adapter disconnected")

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
            logger.exception("MAX webhook invalid json")
            return web.Response(status=400, text="invalid json")
        if not isinstance(update, dict):
            logger.warning("MAX webhook invalid update type=%s", type(update).__name__)
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
        logger.info(
            "MAX webhook accepted update_type=%s chat_id=%s",
            update.get("update_type", "unknown"),
            update.get("chat_id", "unknown"),
        )
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
        if mapped is None:
            return
        if mapped.user_id not in self._allowed_users:
            logger.warning("MAX rejected unauthorized user_id=%s", mapped.user_id)
            return
        if (
            update.get("update_type") in {"message_created", "message_edited"}
            and self._require_mention
            and mapped.chat_type in {"group", "channel"}
        ):
            if not self._bot_username or f"@{self._bot_username}" not in mapped.text:
                return
        text = mapped.text
        if self._bot_username:
            text = text.replace(f"@{self._bot_username}", "").strip()
        callback = update.get("callback") or update.get("message_callback") or {}
        if update.get("update_type") == "message_callback":
            if await self._handle_interactive_callback(mapped, callback):
                return
        logger.info(
            "MAX dispatching update_type=%s chat_id=%s user_id=%s",
            update.get("update_type", "unknown"),
            mapped.chat_id,
            mapped.user_id,
        )
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
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=mapped.message_id,
            raw_message=mapped.raw_update,
            metadata={"edited": mapped.edited},
        )
        await self.handle_message(event)
        if update.get("update_type") == "message_callback" and self._client is not None:
            callback_id = str(callback.get("callback_id") or callback.get("id") or "")
            if callback_id:
                try:
                    await self._client.answer_callback(callback_id)
                except RuntimeError:
                    logger.warning("MAX callback acknowledgement failed")

    async def _register_commands(self) -> None:
        if self._client is None:
            return
        from hermes_cli.commands import (
            COMMAND_REGISTRY,
            _is_gateway_available,
            _resolve_config_gates,
        )

        commands = [
            {"name": command.name, "description": command.description[:128]}
            for command in COMMAND_REGISTRY
            if _is_gateway_available(command, _resolve_config_gates())
        ][:_MAX_COMMAND_LIMIT]
        await self._client.set_commands(commands)
        logger.info("MAX command menu registered commands=%s", len(commands))

    def _next_interaction(
        self,
        chat_id: str,
        message_id: str | None,
        handler: Callable[[str], Awaitable[str | None]],
    ) -> str:
        self._interactive_counter += 1
        interaction_id = str(self._interactive_counter)
        self._interactive[interaction_id] = (chat_id, message_id, handler)
        if len(self._interactive) > _MAX_INTERACTION_LIMIT:
            self._interactive.popitem(last=False)
        return interaction_id

    async def _handle_interactive_callback(self, mapped: Any, callback: dict[str, Any]) -> bool:
        callback_id = str(callback.get("callback_id") or callback.get("id") or "")
        payload = str(callback.get("payload") or "")
        if self._client is None:
            return False
        try:
            if mapped.user_id not in self._allowed_users:
                await self._client.answer_callback(callback_id, "Not authorized")
                return True
            prefix, interaction_id, value = payload.split(":", 2)
            if prefix != "hermes":
                return False
            state = self._interactive.pop(interaction_id, None)
            if state is None or state[0] != mapped.chat_id:
                await self._client.answer_callback(callback_id, "This control has expired")
                return True
            result = await state[2](value)
            await self._client.answer_callback(callback_id)
            if result:
                if state[1]:
                    await self._client.edit_message(state[1], result, [])
                else:
                    await self.send(mapped.chat_id, result)
            return True
        except (ValueError, RuntimeError):
            logger.exception("MAX interactive callback failed")
            if callback_id:
                try:
                    await self._client.answer_callback(callback_id, "Action failed")
                except RuntimeError:
                    logger.warning("MAX callback failure notification failed")
            return True

    async def _send_interaction(
        self,
        chat_id: str,
        text: str,
        buttons: list[tuple[str, str]],
        handler: Callable[[str], Awaitable[str | None]],
        message_id: str | None = None,
        closeable: bool = False,
    ) -> SendResult:
        if len(buttons) + int(closeable) > 30:
            return SendResult(success=False, error="MAX inline keyboards support at most 30 rows")
        if closeable:
            buttons = [*buttons, ("Close", "close")]

            previous_handler = handler

            async def handler(value: str) -> str | None:
                if value == "close":
                    return "Selection cancelled."
                return await previous_handler(value)

        interaction_id = self._next_interaction(chat_id, None, handler)
        rows = [
            [{"type": "callback", "text": label, "payload": f"hermes:{interaction_id}:{value}"}]
            for label, value in buttons
        ]
        if message_id and self._client is not None:
            try:
                await self._client.edit_message(message_id, text, [build_keyboard(rows)])
            except RuntimeError as exc:
                self._interactive.pop(interaction_id, None)
                return SendResult(success=False, error=str(exc))
            result = SendResult(success=True, message_id=message_id)
        else:
            result = await self.send(chat_id, text, metadata={"max_keyboard": rows})
        if not result.success:
            self._interactive.pop(interaction_id, None)
        else:
            self._interactive[interaction_id] = (chat_id, result.message_id, handler)
        return result

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: list | None,
        clarify_id: str,
        session_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if not choices:
            return await self.send(chat_id, question, metadata=metadata)

        async def resolve(value: str) -> str:
            from tools.clarify_gateway import mark_awaiting_text, resolve_gateway_clarify

            if value == "other":
                mark_awaiting_text(clarify_id)
                return "Type your answer."
            resolve_gateway_clarify(clarify_id, str(choices[int(value)]))
            return "Selection received."

        labels = [(str(choice), str(index)) for index, choice in enumerate(choices)]
        labels.append(("Other", "other"))
        return await self._send_interaction(chat_id, question, labels, resolve, closeable=True)

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        async def resolve(choice: str) -> str:
            from tools.slash_confirm import resolve as resolve_confirm

            result = await resolve_confirm(session_key, confirm_id, choice)
            return str(result or "This confirmation has expired.")

        return await self._send_interaction(
            chat_id,
            message,
            [("Approve once", "once"), ("Always approve", "always"), ("Cancel", "cancel")],
            resolve,
        )

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: dict[str, Any] | None = None,
        allow_permanent: bool = True,
        allow_session: bool = True,
        smart_denied: bool = False,
    ) -> SendResult:
        async def resolve(choice: str) -> str:
            from tools.approval import resolve_gateway_approval

            resolved = resolve_gateway_approval(session_key, choice)
            return "Approval received." if resolved else "This approval has expired."

        buttons = [("Allow once", "once"), ("Deny", "deny")]
        if allow_session and not smart_denied:
            buttons.insert(1, ("Allow session", "session"))
        if allow_permanent and not smart_denied:
            buttons.insert(-1, ("Always allow", "always"))
        return await self._send_interaction(
            chat_id,
            f"**Command approval required**\n\n`{command}`\n\n{description}",
            buttons,
            resolve,
        )

    async def send_update_prompt(
        self,
        chat_id: str,
        prompt: str,
        default: str = "",
        session_key: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        async def select(value: str) -> str:
            from hermes_constants import get_hermes_home

            response_path = get_hermes_home() / ".update_response"
            temporary = response_path.with_suffix(".tmp")
            temporary.write_text("y" if value == "yes" else "n", encoding="utf-8")
            temporary.replace(response_path)
            return f"Update prompt answered: **{'Yes' if value == 'yes' else 'No'}**"

        suffix = f" (default: {default})" if default else ""
        return await self._send_interaction(
            chat_id,
            f"**Update needs input**\n\n{prompt}{suffix}",
            [("Yes", "yes"), ("No", "no")],
            select,
        )

    async def send_choice_picker(
        self,
        chat_id: str,
        title: str,
        choices: list,
        session_key: str,
        on_choice_selected: Callable[[str, str], Awaitable[str]],
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        async def resolve(value: str) -> str:
            return await on_choice_selected(chat_id, value)

        buttons = []
        for choice in choices:
            value = str(choice.get("value", ""))
            label = str(choice.get("label") or value)
            if choice.get("is_current"):
                label = f"✓ {label}"
            buttons.append((label, value))
        return await self._send_interaction(chat_id, title, buttons, resolve, closeable=True)

    async def send_model_picker(
        self,
        chat_id: str,
        providers: list,
        current_model: str,
        current_provider: str,
        session_key: str,
        on_model_selected: Callable[[str, str, str], Awaitable[str]],
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        provider_by_slug = {
            str(provider.get("slug") or ""): provider
            for provider in providers
            if provider.get("slug")
        }
        picker: dict[str, str | None] = {"message_id": None}

        async def choose_provider(slug: str) -> str | None:
            provider = provider_by_slug.get(slug)
            if provider is None:
                return "This model picker has expired. Run /model again."
            models = [str(model) for model in provider.get("models") or []]
            if not models:
                return "No models are available for this provider."

            async def back() -> None:
                await self._send_interaction(
                    chat_id,
                    "**Model configuration**\n\n"
                    f"Current: `{current_model or 'unknown'}`\nSelect a provider:",
                    buttons,
                    choose_provider,
                    picker["message_id"],
                    True,
                )

            await self._send_model_page(
                chat_id, slug, models, 0, on_model_selected, picker["message_id"], back
            )
            return None

        buttons = []
        for slug, provider in provider_by_slug.items():
            label = str(provider.get("name") or slug)
            if slug == current_provider:
                label = f"✓ {label}"
            buttons.append((label, slug))
        result = await self._send_interaction(
            chat_id,
            "**Model configuration**\n\n"
            f"Current: `{current_model or 'unknown'}`\nSelect a provider:",
            buttons,
            choose_provider,
            closeable=True,
        )
        picker["message_id"] = result.message_id
        return result

    async def _send_model_page(
        self,
        chat_id: str,
        provider: str,
        models: list[str],
        page: int,
        on_model_selected: Callable[[str, str, str], Awaitable[str]],
        message_id: str | None,
        on_back: Callable[[], Awaitable[None]],
    ) -> SendResult:
        page_size = 8
        total_pages = max(1, (len(models) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        page_models = models[start : start + page_size]

        async def choose_model(value: str) -> str | None:
            if value.startswith("page:"):
                await self._send_model_page(
                    chat_id,
                    provider,
                    models,
                    int(value.removeprefix("page:")),
                    on_model_selected,
                    message_id,
                    on_back,
                )
                return None
            if value == "back":
                await on_back()
                return None
            index = int(value.removeprefix("model:"))
            return await on_model_selected(chat_id, models[index], provider)

        buttons = [
            (model.split("/")[-1][:48], f"model:{start + index}")
            for index, model in enumerate(page_models)
        ]
        if page > 0:
            buttons.append(("Previous", f"page:{page - 1}"))
        if page < total_pages - 1:
            buttons.append(("Next", f"page:{page + 1}"))
        return await self._send_interaction(
            chat_id,
            f"Select a model from `{provider}` ({page + 1}/{total_pages}):",
            buttons,
            choose_model,
            message_id,
            True,
        )

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
            logger.exception("MAX build keyboard failed chat_id=%s", chat_id)
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
                    logger.exception("MAX send message failed chat_id=%s", chat_id)
                    return SendResult(success=False, error=str(exc))
                self._last_send[chat_id] = time.monotonic()
                message_id = response.get("message_id")
                logger.info("MAX sent message chat_id=%s message_id=%s", chat_id, message_id)
        return SendResult(success=True, message_id=message_id)

    async def send_or_update_status(
        self,
        chat_id: str,
        status_key: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        key = (chat_id, status_key)
        message_id = self._status_messages.get(key)
        if message_id and self._client is not None:
            try:
                await self._client.edit_message(message_id, content)
                return SendResult(success=True, message_id=message_id)
            except RuntimeError:
                self._status_messages.pop(key, None)
        result = await self.send(chat_id, content, metadata=metadata)
        if result.success and result.message_id:
            self._status_messages[key] = result.message_id
        return result

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
            logger.exception("MAX send %s failed chat_id=%s", media_type, chat_id)
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
