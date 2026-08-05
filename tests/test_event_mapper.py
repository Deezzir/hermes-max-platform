import pytest

from max_hermes_plugin.event_mapper import build_keyboard, map_update


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
    assert event.chat_id == "9"
    assert event.user_id == "7"
    assert event.chat_type == "dm"


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


def test_keyboard_rejects_invalid_callback_without_payload():
    with pytest.raises(ValueError, match="payload"):
        build_keyboard([[{"type": "callback", "text": "Continue"}]])
