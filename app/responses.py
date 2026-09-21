import re
from pathlib import Path
from string import Formatter
from typing import Literal

from pydantic import Field, StrictBool, model_validator

from app.models import College, DataModel, Group, Lesson, read_json
from app.providers import plain

LessonLabel = Literal["auto", "subject", "teacher", "both"]

# Only named placeholders are allowed. Sizes cover the validated source fields.
TEMPLATE_FIELDS = {
    "help": {},
    "capabilities": {},
    "exit": {},
    "forget": {},
    "my_group": {"group": 200},
    "no_group": {},
    "my_teacher": {"teacher": 200},
    "no_profile": {},
    "ask_profile": {},
    "teacher_saved": {"teacher": 200},
    "ask_group": {},
    "welcome_group": {},
    "ambiguous_group": {"groups": 604},
    "unknown_group": {},
    "group_not_understood": {},
    "group_saved": {"group": 200},
    "welcome": {},
    "unknown_request": {},
    "prefer_subject": {},
    "prefer_teacher": {},
    "prefer_groups": {},
    "prefer_both": {},
    "prefer_auto": {},
    "failure": {},
    "date_multiple": {},
    "date_required": {},
    "date_day_required": {},
    "date_unclear": {},
    "date_invalid": {},
    "date_out_of_range": {},
    "not_published": {},
    "teacher_not_published": {},
    "ask_teacher": {},
    "unknown_teacher": {},
    "ambiguous_teacher": {},
    "multiple_teachers": {},
    "one_target": {},
    "teacher_header": {"teacher": 200},
    "teacher_partial": {},
    "teacher_partial_empty": {},
    "teacher_lesson": {"number": 2, "label": 0},
    "prefer_exit": {},
    "prefer_stay": {},
    "groups": {"groups": 0},
    "page_changed": {},
    "page_more": {},
    "demo": {},
    "stale": {},
    "replacement": {},
    "group_header": {"group": 200},
    "date_header": {"date": 80},
    "day_count": {"count": 20},
    "day_empty": {},
    "pair_empty": {"number": 2},
    "pair_partial_empty": {"number": 2},
    "pair_required": {},
    "pair_invalid": {},
    "pair_multiple": {},
    "subject_missing": {},
    "time_missing": {},
    "day_start": {"time": 5, "number": 2},
    "day_finish": {"time": 5, "number": 2},
    "lesson": {"number": 2, "label": 402},
    "first_lesson": {"number": 2, "label": 402},
    "time_start": {"start": 5},
    "time_range": {"start": 5, "end": 5},
    "unknown_subgroups": {},
    "button_today": {},
    "button_tomorrow": {},
    "button_change": {},
    "button_groups": {},
    "button_teacher": {},
    "button_more": {},
}


class ResponseOptions(DataModel):
    auto_exit: StrictBool = True
    interactive_launch: StrictBool = True
    show_group: StrictBool = False
    teacher_lesson_label: Literal["group", "both"] = "group"
    lesson_label: LessonLabel = "auto"
    course_labels: dict[Literal["1", "2", "3", "4"], Literal["subject", "teacher", "both"]]
    schedule_time: Literal["none", "start", "range"] = "none"
    first_time: Literal["none", "start", "range"] = "start"
    date_format: Literal["none", "short", "full"] = "none"
    page_chars: int = Field(default=700, ge=300, le=1024, strict=True)


class Responses(DataModel):
    schema_version: Literal[1]
    options: ResponseOptions
    texts: dict[str, str]

    @model_validator(mode="after")
    def validate_templates(self):
        if set(self.texts) != set(TEMPLATE_FIELDS):
            raise ValueError("Response template keys must match TEMPLATE_FIELDS")
        if self.texts["help"].strip().casefold() == self.texts["capabilities"].strip().casefold():
            raise ValueError("Help and capabilities must have different texts")
        for key, limits in TEMPLATE_FIELDS.items():
            template = self.texts[key]
            if not template.strip() or len(template) > 600:
                raise ValueError(f"Empty or oversized response template: {key}")
            fields = set()
            for _, name, spec, conversion in Formatter().parse(template):
                if name is None:
                    continue
                if name not in limits or spec or conversion:
                    raise ValueError(f"Unsupported placeholder in response template: {key}")
                fields.add(name)
            if fields != set(limits):
                raise ValueError(f"Missing placeholder in response template: {key}")
            maximum = 64 if key.startswith("button_") else 900
            if key.startswith("page_"):
                maximum = 80
            sample = template.format_map({name: "x" * size for name, size in limits.items()})
            if len(sample) > maximum:
                raise ValueError(f"Rendered response template is too long: {key}")
        return self

    def text(self, key: str, **values) -> str:
        return self.texts[key].format_map(values).strip()

    def label_mode(self, college: College, group: Group, preference: LessonLabel | None):
        mode = preference or self.options.lesson_label
        if mode != "auto":
            return mode
        return self.options.course_labels.get(str(college.course(group.id)), "subject")

    @classmethod
    def load(cls, path: Path):
        return cls.model_validate(read_json(path))


def lesson_label(lesson: Lesson, mode: LessonLabel) -> str:
    subject = plain(lesson.subject)
    teacher = plain(lesson.teacher)
    if teacher.casefold() in {"", "нет", "не указан", "не указано", "—", "-", "н/д"}:
        return subject
    # Strip initials without guessing a person's name or changing surname spelling.
    surname = re.sub(r"[А-ЯЁA-Z]\.\s*", "", teacher)
    surname = re.sub(r"\s+([,;])", r"\1", surname).strip(" ,;")
    if not surname or mode == "subject":
        return subject
    return f"{subject}, {surname}" if mode == "both" else surname
