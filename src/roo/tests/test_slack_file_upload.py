"""Verify SDK payloads without contacting Slack."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import slack_client


@pytest.mark.parametrize("content,key", [("a,b\n1,2", "content"), (b"\x89PNG\r\n\x1a\n", "file"), (b"PK\x03\x04", "file")])
def test_upload_uses_binary_file_or_text_content_and_preserves_thread(monkeypatch, content, key):
    calls = []
    monkeypatch.setattr(slack_client, "get_slack_client", lambda: SimpleNamespace(
        files_upload_v2=lambda **kwargs: calls.append(kwargs) or {"ok": True},
    ))
    response = slack_client.upload_file(
        channel="C123", content=content, filename="report.png", title="Report",
        thread_ts="111.222", initial_comment="Report chart",
    )
    assert response["ok"]
    assert calls == [{
        "channel": "C123", key: content, "filename": "report.png", "title": "Report",
        "thread_ts": "111.222", "initial_comment": "Report chart",
    }]
