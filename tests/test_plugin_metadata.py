from pathlib import Path

import yaml


def test_plugin_metadata_requires_token_url_secret_and_allowlist():
    metadata = yaml.safe_load(Path("plugin.yaml").read_text(encoding="utf-8"))
    required = {item["name"] for item in metadata["requires_env"]}

    assert metadata["kind"] == "platform"
    assert required == {
        "MAX_BOT_TOKEN",
        "MAX_WEBHOOK_URL",
        "MAX_WEBHOOK_SECRET",
        "MAX_ALLOWED_USERS",
    }
    assert "MAX_ALLOW_ALL_USERS" not in str(metadata)
    assert "MAX_OUTBOUND_FORMAT" not in str(metadata)
