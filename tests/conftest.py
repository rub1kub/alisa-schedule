import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.responses import Responses

TODAY = date(2026, 9, 15)
GROUP = "103-Д9-3ИНС"


def envelope(command="", *, state=None, new=False, user=False, payload=None, nlu=None):
    session = {
        "session_id": "test-session",
        "message_id": 1,
        "skill_id": "test-skill",
        "new": new,
        "application": {"application_id": "test-app"},
    }
    if user:
        session["user"] = {"user_id": "test-user"}
    return {
        "version": "1.0",
        "session": session,
        "state": state or {},
        "request": {
            "type": "SimpleUtterance",
            "command": command,
            "payload": payload or {},
            "nlu": nlu or {},
        },
    }


@pytest.fixture
def client(tmp_path):
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"schema_version": 1, "replacements": []}))
    settings = Settings(provider="file", skill_id="test-skill", overrides_file=overrides)
    with TestClient(create_app(settings, now=lambda: TODAY)) as test_client:
        yield test_client


@pytest.fixture
def fixture_data():
    return json.loads(Path("data/schedule.example.json").read_text())


@pytest.fixture
def responses():
    return Responses.load(Path("data/responses.json"))
