from datetime import datetime

import pytest

from tests.conftest import ET
from utils.when import WhenError, describe, parse_when

# Sunday 2026-10-04 15:00 ET
NOW = datetime(2026, 10, 4, 15, 0, tzinfo=ET)
SAT_9PM = datetime(2026, 10, 10, 21, 0, tzinfo=ET)


@pytest.mark.parametrize("text", [
    "sat 9pm", '"sat 9pm"', "'sat 9pm'", "“sat 9pm”", "`sat 9pm`",     # quotes, incl. phone smart quotes
    "Sat 9PM", "saturday 9pm", "Saturday at 9 PM", "sat @ 9pm", "sat, 9pm", "sat 9 pm", "sat 9p",
    "sat 9:00pm", "sat 9.00 pm", "sat 21:00", "sat 2100", "9pm sat", "9pm on saturday", "this sat 9pm",
    "next sat 9pm", "sat 9", "10/10 9pm", "10/10/2026 9pm", "10/10/26 21:00", "oct 10 9pm", "october 10th 9pm",
    "10 oct 9pm", "2026-10-10 21:00", "  sat    9pm  ", "sat at 9p.m.",
])
def test_many_ways_to_say_saturday_9pm(text):
    assert parse_when(text, NOW, ET) == SAT_9PM


@pytest.mark.parametrize("text, expected", [
    ("fri 9pm", datetime(2026, 10, 9, 21, 0, tzinfo=ET)),
    ("Friday 10:30PM", datetime(2026, 10, 9, 22, 30, tzinfo=ET)),
    ("tonight 9pm", datetime(2026, 10, 4, 21, 0, tzinfo=ET)),
    ("tonight 9", datetime(2026, 10, 4, 21, 0, tzinfo=ET)),
    ("tomorrow 21:00", datetime(2026, 10, 5, 21, 0, tzinfo=ET)),
    ("tmrw 10pm", datetime(2026, 10, 5, 22, 0, tzinfo=ET)),
    ("9pm", datetime(2026, 10, 4, 21, 0, tzinfo=ET)),
    ("2pm", datetime(2026, 10, 5, 14, 0, tzinfo=ET)),           # already past today -> tomorrow
    ("sun 2pm", datetime(2026, 10, 11, 14, 0, tzinfo=ET)),      # today's already past -> next week
    ("sun 9pm", datetime(2026, 10, 4, 21, 0, tzinfo=ET)),       # today, still ahead
    ("tomorrow noon", datetime(2026, 10, 5, 12, 0, tzinfo=ET)),
    ("tues 8:30pm", datetime(2026, 10, 6, 20, 30, tzinfo=ET)),
    ("weds 10", datetime(2026, 10, 7, 22, 0, tzinfo=ET)),
])
def test_parse_when(text, expected):
    assert parse_when(text, NOW, ET) == expected


@pytest.mark.parametrize("text, reason", [
    ("", "give a start time"),
    ("someday", "add a time"),
    ("sat", "add a time"),
    ("sat 25pm", "isn't a time"),
    ("sat 9pm please bring snacks", "didn't understand"),
    ("today 1pm", "already passed"),
    ("2026-12-31 21:00", "within the next 30 days"),
    ("2/30 9pm", "isn't a real date"),
    ("oct 9pm", "which day of oct"),
])
def test_parse_when_rejects_with_a_reason(text, reason):
    with pytest.raises(WhenError, match=reason):
        parse_when(text, NOW, ET)


def test_dates_without_a_year_roll_to_next_year():
    assert parse_when("1/5 9pm", NOW, ET, max_days=120) == datetime(2027, 1, 5, 21, 0, tzinfo=ET)


def test_describe_is_what_autocomplete_shows():
    assert describe(SAT_9PM) == "Sat, Oct 10 at 9:00 PM EDT"


@pytest.mark.parametrize("text, hour_et", [
    ("sat 9pm ct", 22), ("sat 9pm central", 22), ("sat 9pm CST", 22), ("sat 9pm pt", 0),
    ("sat 9pm et", 21), ("sat 9pm EST", 21), ("sat 9pm", 21), ("sat 9pm mt", 23),
])
def test_typed_time_zone_is_converted_to_the_bots_zone(text, hour_et):
    result = parse_when(text, NOW, ET)
    assert result.tzinfo == ET or result.utcoffset() == SAT_9PM.utcoffset()
    assert result.astimezone(ET).hour == hour_et


def test_pacific_9pm_saturday_is_sunday_midnight_eastern():
    assert parse_when("sat 9pm pt", NOW, ET) == datetime(2026, 10, 11, 0, 0, tzinfo=ET)


def test_zone_word_does_not_eat_october():
    assert parse_when("oct 10 9pm", NOW, ET) == SAT_9PM


@pytest.mark.parametrize("text, hour, minute", [
    ("sat 9:30", 21, 30), ("sat 21:30", 21, 30), ("sat 09:30", 9, 30), ("sat 0930", 9, 30),
    ("sat 9:30am", 9, 30), ("sat 9:30 pm", 21, 30), ("sat 12:15", 12, 15),
])
def test_minutes_without_am_pm_follow_the_evening_rule(text, hour, minute):
    result = parse_when(text, NOW, ET)
    assert (result.hour, result.minute) == (hour, minute)
