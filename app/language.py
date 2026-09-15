import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.models import Group
from app.protocol import NLU

WEEKDAYS = (
    ("понедельник", "понедельника"),
    ("вторник", "вторника"),
    ("среда", "среду", "среды"),
    ("четверг", "четверга"),
    ("пятница", "пятницу", "пятницы"),
    ("суббота", "субботу", "субботы"),
    ("воскресенье", "воскресенья"),
)
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
NUMBERS = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
    "двести": 200,
    "триста": 300,
    "четыреста": 400,
    "пятьсот": 500,
    "шестьсот": 600,
    "семьсот": 700,
    "восемьсот": 800,
    "девятьсот": 900,
}
ORDINALS = (
    ("перв", 1),
    ("втор", 2),
    ("трет", 3),
    ("четверт", 4),
    ("пят", 5),
    ("шест", 6),
    ("седьм", 7),
    ("восьм", 8),
    ("девят", 9),
    ("десят", 10),
    ("одиннадцат", 11),
    ("двенадцат", 12),
    ("тринадцат", 13),
    ("четырнадцат", 14),
    ("пятнадцат", 15),
    ("шестнадцат", 16),
    ("семнадцат", 17),
    ("восемнадцат", 18),
    ("девятнадцат", 19),
    ("двадцат", 20),
    ("тридцат", 30),
)


def normalize(text: str) -> str:
    text = text.casefold().replace("ё", "е")
    text = re.sub(r"(?<=[а-яa-z])(?=\d)|(?<=\d)(?=[а-яa-z])", " ", text)
    return " ".join(re.sub(r"[^а-яa-z0-9]+", " ", text).split())


def number_words(text: str) -> str:
    result: list[str] = []
    current: int | None = None
    for word in normalize(text).split():
        value = NUMBERS.get(word)
        if word in {"третья", "третье", "третьей", "третью", "третьего"}:
            value = 3
        if value is None:
            for stem, number in ORDINALS:
                if re.fullmatch(stem + r"(?:ый|ой|ий|ая|яя|ое|ее|ого|его|ую|юю|ей)", word):
                    value = number
                    break
        if value is not None:
            current = (current or 0) + value
        else:
            if current is not None:
                result.append(str(current))
                current = None
            result.append(word)
    if current is not None:
        result.append(str(current))
    return " ".join(result)


def group_matches(command: str, groups: list[Group], allow_short: bool) -> list[Group]:
    text = " " + number_words(command) + " "
    full = []
    for group in groups:
        aliases = [group.id, group.name, *group.aliases]
        if any(" " + number_words(alias) + " " in text for alias in aliases):
            full.append(group)
    if full or not allow_short:
        return full
    # Shared prefixes (e.g. the two groups 103) intentionally produce ambiguity.
    return [
        group
        for group in groups
        if (prefix := re.match(r"^\d+", group.name)) and f" {prefix[0]} " in text
    ]


@dataclass(frozen=True)
class DateSelection:
    value: date | None = None
    error: str | None = None


def _calendar_date(day: int, month: int, year: int) -> date:
    return date(year, month, day)


def parse_date(command: str, nlu: NLU, today: date) -> DateSelection:
    text = normalize(command)
    try:
        relative = {"сегодня": 0, "завтра": 1, "послезавтра": 2, "вчера": -1, "позавчера": -2}
        days = {
            today + timedelta(days=offset)
            for word, offset in relative.items()
            if word in text.split()
        }
        found_weekdays = [
            index
            for index, forms in enumerate(WEEKDAYS)
            if any(form in text.split() for form in forms)
        ]
        for weekday in found_weekdays:
            monday = today - timedelta(days=today.weekday())
            if re.search(r"\bследующ\w*", text):
                days.add(monday + timedelta(days=7 + weekday))
            elif re.search(r"\bпрошл\w*", text):
                days.add(monday + timedelta(days=-7 + weekday))
            elif re.search(r"\b(?:этот|эту|это|этой|текущ\w*)\b", text):
                days.add(monday + timedelta(days=weekday))
            else:
                days.add(today + timedelta(days=(weekday - today.weekday()) % 7))
        if len(days) > 1:
            return DateSelection(error="date_multiple")
        if days:
            return DateSelection(value=days.pop())

        iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", command)
        if iso:
            return DateSelection(value=date(int(iso[1]), int(iso[2]), int(iso[3])))
        numeric = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?\b", command)
        if numeric:
            day, month = int(numeric[1]), int(numeric[2])
            year = int(numeric[3]) if numeric[3] else today.year
            value = _calendar_date(day, month, year)
            if not numeric[3] and value < today:
                value = _calendar_date(day, month, year + 1)
            return DateSelection(value=value)

        # Yandex resolves spoken dates such as "двадцать пятого сентября".
        entities = [
            entity.get("value")
            for entity in nlu.entities
            if entity.get("type") == "YANDEX.DATETIME"
        ]
        if len(entities) > 1:
            return DateSelection(error="date_multiple")
        if entities:
            value = entities[0]
            if not isinstance(value, dict) or not any(k in value for k in ("year", "month", "day")):
                return DateSelection(error="date_required")
            for key in ("year", "month", "day"):
                if key in value and type(value[key]) is not int:
                    raise ValueError("Invalid date entity")
            year = value.get("year", today.year)
            if value.get("year_is_relative"):
                year = today.year + value.get("year", 0)
            month = value.get("month", today.month)
            if value.get("month_is_relative"):
                total = year * 12 + today.month - 1 + value.get("month", 0)
                year, month0 = divmod(total, 12)
                month = month0 + 1
            if value.get("day_is_relative"):
                base = date(year, month, min(today.day, calendar.monthrange(year, month)[1]))
                target = base + timedelta(days=value.get("day", 0))
            elif "day" in value:
                target = date(year, month, value["day"])
                if "year" not in value and "month" in value and target < today:
                    target = date(year + 1, month, value["day"])
            else:
                return DateSelection(error="date_day_required")
            return DateSelection(value=target)

        spoken = number_words(command)
        for index, month_name in enumerate(MONTHS, 1):
            match = re.search(rf"\b(\d{{1,2}}) {month_name}(?: (\d{{4}}))?\b", spoken)
            if match:
                year = int(match[2]) if match[2] else today.year
                target = date(year, index, int(match[1]))
                if not match[2] and target < today:
                    target = date(year + 1, index, int(match[1]))
                return DateSelection(value=target)
        match = re.search(r"\bчерез (\d{1,3}) (?:день|дня|дней)\b", spoken)
        if match:
            return DateSelection(value=today + timedelta(days=int(match[1])))
        if re.search(r"\b(?:недел\w*|месяц\w*|числ\w*)\b", text) or any(m in text for m in MONTHS):
            return DateSelection(error="date_unclear")
        if " на " in f" {text} " and not re.search(r"\bгрупп\w*", text):
            return DateSelection(error="date_unclear")
        return DateSelection()
    except (ValueError, OverflowError):
        return DateSelection(error="date_invalid")


def detect_intent(command: str, nlu: NLU) -> str | None:
    text = normalize(command)
    if "YANDEX.HELP" in nlu.intents or text in {"помощь", "что ты умеешь", "что ты можешь"}:
        return "help"
    if text in {"хватит", "стоп", "выход", "закрой навык", "до свидания"}:
        return "exit"
    preferences = {
        "prefer_exit": {"выходи после ответа", "завершай после ответа", "включи автовыход"},
        "prefer_stay": {"не выходи после ответа", "оставайся в навыке", "выключи автовыход"},
        "prefer_subject": {"называй предметы", "только предметы", "предметы", "по предметам"},
        "prefer_teacher": {
            "называй преподавателей",
            "называй фамилии",
            "только преподаватели",
            "преподаватели",
            "фамилии преподавателей",
            "по преподавателям",
            "фамилии",
        },
        "prefer_both": {"предметы и преподаватели", "называй предметы и преподавателей"},
        "prefer_auto": {"выбирай по курсу", "автоматический режим", "режим по курсу"},
    }
    for intent, commands in preferences.items():
        if text in commands:
            return intent
    if re.search(r"\b(?:забудь|сбрось|удали)\b.*\bгрупп\w*", text):
        return "forget"
    if re.search(
        r"\b(?:сменить|смена|смени|поменяй|выбрать|изменить|другая|запомни)\b.*\bгрупп\w*", text
    ):
        return "change"
    if text in {"моя группа", "какая у меня группа"}:
        return "my_group"
    if text in {"группы", "список групп", "какие есть группы"}:
        return "groups"
    if text in {"дальше", "еще", "продолжай", "продолжить"}:
        return "more"
    if re.search(r"\bсколько\b.*\b(?:пар|пары|занятий|уроков)\b", text):
        return "count"
    if (
        re.search(r"\b(?:первая|первую|первое|1(?:\s*я)?)\s+(?:пара|пару|занятие)\b", text)
        or text in {"первая", "первую", "первое"}
        or "во сколько начало" in text
    ):
        return "first"
    if re.search(
        r"\b(?:расписание|пары|пара|занятия|учимся|учеба|что|предмет\w*|препод\w*|фамили\w*)\b",
        text,
    ):
        return "schedule"
    return None


def requested_label(command: str) -> str | None:
    text = normalize(command)
    subjects = bool(re.search(r"\bпредмет\w*", text))
    teachers = bool(re.search(r"\b(?:препод\w*|фамили\w*|кто)\b", text))
    if subjects and teachers:
        return "both"
    return "subject" if subjects else "teacher" if teachers else None


def date_label(value: date, *, full: bool = False) -> str:
    label = f"{value.day} {MONTHS[value.month - 1]}"
    return f"{label} {value.year} года, {WEEKDAYS[value.weekday()][0]}" if full else label


def pair_count(number: int) -> str:
    form = (
        "пар"
        if 11 <= number % 100 <= 14
        else ("пара" if number % 10 == 1 else "пары" if number % 10 in {2, 3, 4} else "пар")
    )
    return f"{number} {form}"
