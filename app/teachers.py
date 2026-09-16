"""Conservative name matching; ambiguous surnames always need clarification."""

import re

from app.language import MONTHS, WEEKDAYS, normalize, number_words
from app.models import Teacher


def teacher_key(name: str) -> str:
    return normalize(name)


def teacher_names(value: str) -> list[str]:
    return [name.strip() for name in re.split(r"[;,]", value) if name.strip()]


def surname(name: str) -> str:
    # Catalog entries contain a surname and initials, never expand initials into names.
    return re.sub(r"(?<![А-ЯЁA-Z])[А-ЯЁA-Z]\.\s*", "", name).strip(" ,;")


def surname_forms(name: str) -> set[str]:
    base = normalize(surname(name))
    forms = {base}
    if base.endswith(("ова", "ева", "ина", "ына")):
        forms.update({base[:-1] + "ой", base[:-1] + "у"})
    elif base.endswith(("ов", "ев", "ин", "ын")):
        forms.update(base + ending for ending in ("а", "у", "ым", "е"))
    elif base.endswith(("ский", "цкий")):
        forms.update(base[:-2] + ending for ending in ("ого", "ому", "им", "ом"))
    elif base.endswith("ый"):
        forms.update(base[:-2] + ending for ending in ("ого", "ому", "ым", "ом"))
    elif base.endswith("ая"):
        forms.update({base[:-2] + "ой", base[:-2] + "ую"})
    elif base.endswith("ой"):
        forms.update(base[:-2] + ending for ending in ("ого", "ому", "ым", "ом"))
    elif base.endswith("ь"):
        forms.update(base[:-1] + ending for ending in ("я", "ю", "ем", "е"))
    elif re.search(r"[бвгджзклмнпрстфхцчшщ]$", base):
        forms.update(base + ending for ending in ("а", "у", "ом", "е"))
    return forms


def teacher_matches(command: str, teachers: list[Teacher]) -> list[Teacher]:
    text = " " + normalize(command) + " "
    full, partial = [], []
    for teacher in teachers:
        aliases = [teacher.name, *teacher.aliases]
        if any(" " + normalize(alias) + " " in text for alias in aliases):
            full.append(teacher)
        elif any(" " + form + " " in text for form in surname_forms(teacher.name)):
            # Initials may follow an inflected surname: "у Иванова А. Б.".
            initials = (
                normalize(teacher.name).removeprefix(normalize(surname(teacher.name))).strip()
            )
            if initials and any(
                f" {form} {initials} " in text for form in surname_forms(teacher.name)
            ):
                full.append(teacher)
            elif not any(
                re.search(r" " + re.escape(form) + r" [а-яa-z] [а-яa-z] ", text)
                for form in surname_forms(teacher.name)
            ):
                partial.append(teacher)
    return full or partial


def teacher_query(command: str) -> str | None:
    """Return a name fragment only for a teacher lookup, not 'кто ведёт пару'."""
    text = normalize(command)
    if re.fullmatch(r"(?:а )?(?:какой )?(?:преподаватель|препод|кто)", text):
        return None
    marker = re.search(r"\b(?:преподавателя|преподавателю|преподаватель|препода)\b(.*)", text)
    if marker:
        fragment = marker[1].strip()
        # 'Преподаватель второй пары' describes a lesson; a surname after
        # 'преподавателя' is still an explicit timetable target.
        if re.match(
            r"(?:(?:на\s+)?\d+(?:\s+(?:я|й|ю|ой|ей))?\s+пар\w*|ведет|ведут)\b",
            number_words(fragment),
        ):
            return None
        return fragment
    if re.search(r"\bгрупп\w*", text):
        return None
    owner = re.search(r"\bу\s+(?!меня\b|нас\b|тебя\b|вас\b)(.+)", text)
    if owner:
        candidate = owner[1]
        if not re.match(r"(?:групп\w*\b|\d)", number_words(candidate)):
            return candidate
        return None
    direct = re.search(r"\bрасписание\s+(.+)", text)
    if direct:
        stop = {
            "на",
            "сегодня",
            "завтра",
            "послезавтра",
            "вчера",
            "мое",
            "моё",
            "мне",
            "пожалуйста",
            "через",
            "с",
            "со",
            "в",
            "за",
            "без",
            "по",
            "не",
        }
        stop.update(MONTHS)
        stop.update(form for forms in WEEKDAYS for form in forms)
        first = direct[1].split()[0]
        if first not in stop and not re.match(r"групп|\d", number_words(first)):
            return direct[1]
    return None
