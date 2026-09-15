import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=200)]
ClockTime = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


class DataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Bell(DataModel):
    start: ClockTime
    end: ClockTime

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("Lesson must end after it starts")
        return self


class College(DataModel):
    name: Text
    timezone: str
    bells: dict[Literal["weekday", "saturday"], dict[str, Bell]] = Field(default_factory=dict)
    group_aliases: dict[str, list[Text]] = Field(default_factory=dict)
    excluded_groups: list[Text] = Field(default_factory=list)
    subject_aliases: dict[str, Text] = Field(default_factory=dict)
    group_courses: dict[str, Annotated[int, Field(ge=1, le=4, strict=True)]] = Field(
        default_factory=dict
    )

    def course(self, group_id: str) -> int | None:
        if group_id in self.group_courses:
            return self.group_courses[group_id]
        match = re.search(r"-([1-4])[А-ЯЁA-Z]", group_id, re.I)
        return int(match[1]) if match else None

    @model_validator(mode="after")
    def valid_timezone_and_bells(self):
        ZoneInfo(self.timezone)
        for table in self.bells.values():
            if any(not re.fullmatch(r"(?:[1-9]|1[0-2])", number) for number in table):
                raise ValueError("Bell number must be between 1 and 12")
        return self


class Group(DataModel):
    id: Text
    name: Text
    aliases: list[Text] = Field(default_factory=list)


class Lesson(DataModel):
    number: int = Field(ge=1, le=12, strict=True)
    subject: Text
    teacher: str = Field(default="", max_length=200)
    room: str = Field(default="", max_length=100)
    subgroup: str = Field(default="", max_length=80)
    start: ClockTime | None = None
    end: ClockTime | None = None

    @model_validator(mode="after")
    def valid_time(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("Provide both start and end")
        if self.start and self.end and self.start >= self.end:
            raise ValueError("Lesson must end after it starts")
        return self


class ScheduleDay(DataModel):
    group_id: Text
    date: date
    lessons: list[Lesson] = Field(max_length=48)
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def no_duplicates(self):
        seen = set()
        for lesson in self.lessons:
            key = (lesson.number, lesson.subgroup)
            if key in seen:
                raise ValueError("Repeated pair requires distinct subgroup labels")
            seen.add(key)
        return self


class ScheduleResult(ScheduleDay):
    source: Literal["kkepik", "file", "override"]
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stale: bool = False
    demo: bool = False


class ScheduleFile(DataModel):
    schema_version: Literal[1]
    demo: bool = False
    groups: list[Group] = Field(min_length=1, max_length=5000)
    days: list[ScheduleDay] = Field(max_length=20000)

    @model_validator(mode="after")
    def references(self):
        group_ids = {group.id for group in self.groups}
        if len(group_ids) != len(self.groups):
            raise ValueError("Duplicate group id")
        validate_unique_days(self.days)
        if any(day.group_id not in group_ids for day in self.days):
            raise ValueError("Unknown group in schedule")
        return self


class Overrides(DataModel):
    schema_version: Literal[1]
    replacements: list[ScheduleDay] = Field(default_factory=list, max_length=20000)

    @model_validator(mode="after")
    def unique(self):
        validate_unique_days(self.replacements)
        return self


def validate_unique_days(days: list[ScheduleDay]):
    keys = [(day.group_id, day.date) for day in days]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate group/date")


def read_json(path: Path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Data file exceeds 8 MiB")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
