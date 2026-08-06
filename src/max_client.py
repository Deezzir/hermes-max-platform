from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import aiohttp
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


class MaxApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = status in {429, 503}


class MaxClient:
    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://platform-api2.max.ru",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base = api_base.rstrip("/")
        self._sleep = sleep
        self._session = aiohttp.ClientSession(headers={"Authorization": token})
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def close(self) -> None:
        await self._session.close()

    @staticmethod
    def _is_retryable(error: BaseException) -> bool:
        return isinstance(error, MaxApiError) and error.retryable

    async def _request_once(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            delay = max(0.0, 1 / 30 - (now - self._last_request))
            if delay:
                await self._sleep(delay)
            self._last_request = asyncio.get_running_loop().time()
            try:
                async with self._session.request(
                    method, f"{self._base}{path}", **kwargs
                ) as response:
                    data = cast(dict[str, Any], await response.json(content_type=None))
            except aiohttp.ClientError as exc:
                raise MaxApiError(503, str(exc)) from exc
        if response.status < 400:
            return data
        message = str(data.get("message", data)) if isinstance(data, dict) else str(data)
        raise MaxApiError(response.status, message)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        retrying = retry(
            retry=retry_if_exception(self._is_retryable),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=0, max=4),
            sleep=self._sleep,
            reraise=True,
        )(self._request_once)
        return cast(dict[str, Any], await retrying(method, path, **kwargs))

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def set_commands(self, commands: list[dict[str, str]]) -> dict[str, Any]:
        return await self._request("PATCH", "/me/commands", json={"commands": commands})

    async def register_subscription(
        self, url: str, secret: str, update_types: list[str]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/subscriptions",
            json={"url": url, "secret": secret, "update_types": update_types},
        )

    async def send_message(
        self,
        *,
        chat_id: str | None = None,
        user_id: str | None = None,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        if text is not None and len(text) > 4000:
            raise ValueError("MAX messages are limited to 4000 characters")
        params = {
            key: value for key, value in {"chat_id": chat_id, "user_id": user_id}.items() if value
        }
        body: dict[str, Any] = {}
        if text is not None:
            body.update(text=text, format="markdown")
        if attachments:
            body["attachments"] = attachments
        if reply_to:
            body["link"] = {"type": "reply", "message_id": reply_to}
        data = await self._request("POST", "/messages", params=params, json=body)
        return cast(dict[str, Any], data.get("message", data))

    async def edit_message(
        self, message_id: str, text: str, attachments: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "format": "markdown"}
        if attachments is not None:
            body["attachments"] = attachments
        return await self._request("PUT", "/messages", params={"message_id": message_id}, json=body)

    async def send_action(self, chat_id: str, action: str = "typing_on") -> None:
        await self._request("POST", f"/chats/{chat_id}/actions", json={"action": action})

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        body: dict[str, Any] = {"notification": text or "OK"}
        await self._request("POST", "/answers", params={"callback_id": callback_id}, json=body)

    async def upload_file(self, path: Path, media_type: str) -> dict[str, Any]:
        upload = await self._request("POST", "/uploads", params={"type": media_type})
        form = aiohttp.FormData()
        with path.open("rb") as file:
            form.add_field("data", file, filename=path.name)
            async with self._session.post(upload["url"], data=form) as response:
                data = cast(dict[str, Any], await response.json(content_type=None))
                if response.status >= 400:
                    message = (
                        str(data.get("message", data)) if isinstance(data, dict) else str(data)
                    )
                    raise MaxApiError(response.status, message)
        return data
