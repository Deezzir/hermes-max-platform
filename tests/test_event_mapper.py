import pytest

from src.event_mapper import build_keyboard, map_update


def test_message_created_maps_direct_message():
    event = map_update(
        {
            "update_type": "message_created",
            "chat_id": 9,
            "message": {
                "body": {"text": "hello"},
                "sender": {"user_id": 7, "name": "Ada"},
                "recipient": {"chat_id": 9},
            },
        }
    )

    assert event.text == "hello"
    assert event.chat_id == "7"
    assert event.user_id == "7"
    assert event.chat_type == "dm"


def test_direct_message_without_top_level_chat_id_replies_to_sender():
    event = map_update(
        {
            "update_type": "message_created",
            "message": {
                "body": {"text": "hello"},
                "sender": {"user_id": 7, "name": "Ada"},
                "recipient": {"chat_id": 9},
            },
        }
    )

    assert event.chat_id == "7"
    assert event.chat_type == "dm"


def test_group_message_without_recipient_type_replies_to_group():
    event = map_update(
        {
            "update_type": "message_created",
            "message": {
                "body": {"text": "hello"},
                "sender": {"user_id": 7, "name": "Ada"},
                "recipient": {"chat_id": -77592070320345},
            },
        }
    )

    assert event.chat_id == "-77592070320345"
    assert event.chat_type == "group"


def test_callback_maps_to_tagged_text():
    event = map_update(
        {
            "update_type": "message_callback",
            "callback": {"payload": "continue"},
            "chat_id": 9,
            "user": {"user_id": 7, "name": "Ada"},
        }
    )

    assert event.text == "[MAX callback payload: continue]"


def test_callback_uses_clicking_user_for_authorization():
    event = map_update(
        {
            "update_type": "message_callback",
            "callback": {"payload": "continue", "user": {"user_id": 8, "name": "Grace"}},
            "message": {
                "sender": {"user_id": 7, "name": "Ada"},
                "recipient": {"chat_id": 9},
            },
        }
    )

    assert event.user_id == "8"
    assert event.user_name == "Grace"


def test_keyboard_rejects_invalid_callback_without_payload():
    with pytest.raises(ValueError, match="payload"):
        build_keyboard([[{"type": "callback", "text": "Continue"}]])


def test_mapper_handles_attachment_bot_and_unsupported_updates():
    attachment = map_update(
        {
            "update_type": "message_created",
            "message": {
                "body": {"attachments": [{"type": "file"}, {"type": "contact"}]},
                "sender": {"user_id": 7},
                "recipient": {"chat_id": 9},
            },
        }
    )
    started = map_update(
        {"update_type": "bot_started", "payload": "invite", "user": {"user_id": 7}}
    )

    assert attachment.text == "[MAX file, MAX contact redacted]"
    assert started.text == "[MAX bot started: invite]"
    assert map_update({"update_type": "unknown"}) is None


def test_keyboard_builds_valid_rows():
    assert build_keyboard([[{"type": "callback", "text": "Continue", "payload": "continue"}]]) == {
        "type": "inline_keyboard",
        "payload": {"buttons": [[{"type": "callback", "text": "Continue", "payload": "continue"}]]},
    }
