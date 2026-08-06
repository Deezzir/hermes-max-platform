from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundEvent:
    text: str
    chat_id: str
    chat_type: str
    user_id: str
    user_name: str
    message_id: str | None
    raw_update: dict[str, Any]
    edited: bool = False


_BUTTON_FIELDS = {
    "callback": ("payload",),
    "link": ("url",),
    "message": ("payload",),
    "clipboard": ("payload",),
    "request_contact": (),
    "request_geo_location": (),
    "open_app": ("url",),
}
_THREE_BUTTON_TYPES = {"link", "open_app", "request_contact", "request_geo_location"}

logger = logging.getLogger(__name__)


def _message(update: dict[str, Any]) -> dict[str, Any]:
    return update.get("message") or update.get("message_created") or {}


def _attachment_text(attachments: list[dict[str, Any]]) -> str:
    labels = []
    for attachment in attachments:
        kind = attachment.get("type", "attachment")
        labels.append("MAX contact redacted" if kind == "contact" else f"MAX {kind}")
    return "[" + ", ".join(labels) + "]" if labels else "[MAX message]"


def map_update(update: dict[str, Any]) -> InboundEvent | None:
    kind = update.get("update_type")
    message = _message(update)
    user = message.get("sender") or update.get("user") or {}
    body = message.get("body") or {}
    recipient = message.get("recipient") or {}
    chat_id = str(update.get("chat_id") or body.get("chat_id") or "")
    is_channel = bool(update.get("is_channel") or recipient.get("is_channel"))
    chat_type = "channel" if is_channel else "dm"
    recipient_chat_id = str(recipient.get("chat_id") or "")
    if not is_channel and (recipient.get("type") == "chat" or recipient_chat_id.startswith("-")):
        chat_type = "group"
        chat_id = recipient_chat_id
    user_id = str(user.get("user_id") or "")
    if chat_type == "dm":
        chat_id = user_id
    user_name = str(user.get("name") or user_id)
    logger.info(
        "MAX mapping update_type=%s top_level_chat_id=%s body_chat_id=%s "
        "recipient_chat_id=%s recipient_type=%s is_channel=%s sender_user_id=%s "
        "resolved_chat_id=%s chat_type=%s",
        kind,
        update.get("chat_id"),
        body.get("chat_id"),
        recipient.get("chat_id"),
        recipient.get("type"),
        is_channel,
        user_id,
        chat_id,
        chat_type,
    )
    if kind == "bot_started":
        payload = update.get("payload")
        text = f"[MAX bot started: {payload}]" if payload else "[MAX bot started]"
    elif kind == "message_callback":
        callback = update.get("callback") or update.get("message_callback") or {}
        text = f"[MAX callback payload: {callback.get('payload', '')}]"
    elif kind in {"message_created", "message_edited"}:
        attachments = body.get("attachments") or []
        text = body.get("text") or _attachment_text(attachments)
    else:
        logger.info("MAX ignoring unsupported update_type=%s", kind)
        return None
    if not chat_id:
        logger.warning("MAX mapped update without a chat_id update_type=%s", kind)
    logger.info(
        "MAX mapped update_type=%s chat_id=%s chat_type=%s sender_user_id=%s "
        "message_id=%s edited=%s",
        kind,
        chat_id,
        chat_type,
        user_id,
        message.get("message_id"),
        kind == "message_edited",
    )
    return InboundEvent(
        text,
        chat_id,
        chat_type,
        user_id,
        user_name,
        str(message.get("message_id") or "") or None,
        update,
        kind == "message_edited",
    )


def event_fingerprint(update: dict[str, Any]) -> str:
    payload = json.dumps(update, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def build_keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    if len(rows) > 30:
        raise ValueError("MAX keyboards support at most 30 rows")
    rendered = []
    for row in rows:
        if not row or len(row) > 7:
            raise ValueError("MAX keyboard rows require 1 to 7 buttons")
        special = any(button.get("type") in _THREE_BUTTON_TYPES for button in row)
        if special and len(row) > 3:
            raise ValueError("rows with link or request buttons allow at most 3 buttons")
        result = []
        for button in row:
            button_type = button.get("type")
            if button_type not in _BUTTON_FIELDS or not button.get("text"):
                raise ValueError("button type and text are required")
            for field in _BUTTON_FIELDS[button_type]:
                if not button.get(field):
                    raise ValueError(f"{button_type} button requires {field}")
            result.append(dict(button))
        rendered.append(result)
    return {"type": "inline_keyboard", "payload": {"buttons": rendered}}
