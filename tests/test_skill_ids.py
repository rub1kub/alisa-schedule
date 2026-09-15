import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from tests.conftest import GROUP, TODAY, envelope


def test_production_requires_primary_id_even_with_test_skill():
    with pytest.raises(ValidationError, match="ALICE_SKILL_ID is required"):
        Settings(environment="production", test_skill_id="private-skill")


def test_skill_ids_load_from_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALICE_SKILL_ID", " public-skill ")
    monkeypatch.setenv("ALICE_TEST_SKILL_ID", " private-skill ")
    settings = Settings.from_env()
    assert (settings.skill_id, settings.test_skill_id) == ("public-skill", "private-skill")


@pytest.mark.parametrize("private_id", ["", "private-skill"])
def test_only_configured_skills_are_accepted(private_id):
    settings = Settings(provider="file", skill_id="public-skill", test_skill_id=private_id)
    with TestClient(create_app(settings, now=lambda: TODAY)) as client:
        for skill_id, expected in (
            ("public-skill", 200),
            ("private-skill", 200 if private_id else 403),
            ("unknown-skill", 403),
        ):
            payload = envelope(new=True)
            payload["session"]["skill_id"] = skill_id
            assert client.post("/webhook", json=payload).status_code == expected


def test_group_state_does_not_leak_between_skills():
    settings = Settings(provider="file", skill_id="public-skill", test_skill_id="private-skill")
    with TestClient(create_app(settings, now=lambda: TODAY)) as client:
        payload = envelope("группа сто три", new=True)
        payload["session"]["skill_id"] = "public-skill"
        response = client.post("/webhook", json=payload).json()
        assert response["application_state"]["group_id"] == GROUP
        payload = envelope("что завтра", new=True)
        payload["session"]["skill_id"] = "private-skill"
        response = client.post("/webhook", json=payload).json()
        assert not response.get("session_state", {}).get("group_id")
        assert "групп" in response["response"]["text"].lower()
