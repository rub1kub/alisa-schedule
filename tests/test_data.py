import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.dialog import Skill
from app.models import College, Lesson, Overrides, ScheduleFile, read_json


def test_production_requires_configured_skill_id():
    with pytest.raises(ValidationError):
        Settings(environment="production")
    assert Settings(environment="production", skill_id="configured-skill").skill_id


def test_moscow_date_is_independent_of_server_and_device_timezone(responses):
    college = College.model_validate(read_json(Path("data/college.json")))
    instant = datetime(2026, 9, 15, 21, 30, tzinfo=UTC)
    with patch("app.dialog.datetime") as clock:
        clock.now.side_effect = lambda tz: instant.astimezone(tz)
        skill = Skill(None, college, responses)
        assert skill.now() == date(2026, 9, 16)


def test_duplicate_json_keys_are_rejected(tmp_path):
    data = tmp_path / "input.json"
    data.write_text('{"schema_version":1,"schema_version":2}')
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        read_json(data)


def test_duplicate_or_unknown_days_are_rejected(fixture_data):
    duplicate = json.loads(json.dumps(fixture_data))
    duplicate["days"].append(duplicate["days"][0])
    with pytest.raises(ValidationError):
        ScheduleFile.model_validate(duplicate)
    fixture_data["days"][0]["group_id"] = "unknown"
    with pytest.raises(ValidationError):
        ScheduleFile.model_validate(fixture_data)


def test_invalid_time_and_duplicate_override_are_rejected():
    with pytest.raises(ValidationError):
        Lesson(number=1, subject="Математика", start="11:00", end="10:00")
    with pytest.raises(ValidationError):
        Lesson(number=1, subject="Математика", start="11:00")
    day = {"group_id": "103-Д9-3ИНС", "date": "2026-09-15", "lessons": []}
    with pytest.raises(ValidationError):
        Overrides(schema_version=1, replacements=[day, day])
