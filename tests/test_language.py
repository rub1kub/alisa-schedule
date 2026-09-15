from datetime import date

import pytest

from app.language import detect_intent, group_matches, parse_date
from app.models import Group
from app.protocol import NLU


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("завтра", date(2026, 1, 1)),
        ("в пятницу", date(2026, 1, 2)),
        ("в следующую пятницу", date(2026, 1, 9)),
        ("на 2 января", date(2026, 1, 2)),
        ("на 02.01", date(2026, 1, 2)),
        ("2026-01-02", date(2026, 1, 2)),
        ("через три дня", date(2026, 1, 3)),
        ("на двадцать пятое января", date(2026, 1, 25)),
    ],
)
def test_dates_cross_year(text, expected):
    assert parse_date(text, NLU(), date(2025, 12, 31)).value == expected


def test_weekday_today_and_conflicting_days():
    today = date(2026, 9, 18)
    assert parse_date("пятница", NLU(), today).value == today
    assert parse_date("сегодня или завтра", NLU(), today).error
    assert parse_date("на следующей неделе", NLU(), today).error


def test_yandex_relative_and_absolute_dates():
    nlu = NLU(entities=[{"type": "YANDEX.DATETIME", "value": {"day": 2, "day_is_relative": True}}])
    assert parse_date("через пару дней", nlu, date(2026, 9, 15)).value == date(2026, 9, 17)
    nlu = NLU(entities=[{"type": "YANDEX.DATETIME", "value": {"day": 25, "month": 9}}])
    assert parse_date("двадцать пятого сентября", nlu, date(2026, 9, 15)).value == date(2026, 9, 25)
    nlu = NLU(entities=[{"type": "YANDEX.DATETIME", "value": {"day": 31, "month": 2}}])
    assert parse_date("дата", nlu, date(2026, 9, 15)).error


def test_group_numbers_spoken_as_cardinals_ordinals_or_code():
    group = Group(id="103-Д9-3ИНС", name="103-Д9-3ИНС")
    for phrase in ("103", "группа сто три", "сто третья", "103 д9 3инс"):
        assert group_matches(phrase, [group], True) == [group]
    assert not group_matches("расписание на 103", [group], False)


def test_ambiguous_prefix_never_silently_picks_first():
    groups = [Group(id="21-А", name="21-А"), Group(id="21-Б", name="21-Б")]
    assert len(group_matches("группа двадцать один", groups, True)) == 2
    assert group_matches("21-а", groups, True) == groups[:1]


def test_ordinal_group_is_not_a_question_about_the_first_pair():
    assert detect_intent("группа сто первая", NLU()) is None
    assert detect_intent("какая первая пара", NLU()) == "first"


def test_unrecognized_date_does_not_fall_back_to_today():
    assert parse_date("расписание на март", NLU(), date(2026, 9, 15)).error
