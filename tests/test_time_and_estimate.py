import datetime
import os
import unittest
from zoneinfo import ZoneInfo

import stubs  # noqa: F401
from stubs import ClientError

for name in ("SCORES_TABLE", "AWARDS_TABLE", "CATALOG_TABLE"):
    os.environ.setdefault(name, "t")
import scoring_engine as se
import tasks_api as ta
import weekly_closer as wc
import leaderboard_api as lb

CAIRO = ZoneInfo("Africa/Cairo")
UTC = datetime.timezone.utc


def epoch(*args, tz=UTC):
    return int(datetime.datetime(*args, tzinfo=tz).timestamp())


class CairoBoundaryTests(unittest.TestCase):
    def test_winter_sunday_late_evening_utc_is_already_monday_in_cairo(self):
        # 22:30 UTC on Sunday 22 Nov 2026 is 00:30 Monday 23 Nov in Cairo (UTC+2)
        self.assertEqual(se.week_and_day(epoch(2026, 11, 22, 22, 30)), ("2026-W48", "2026-11-23"))
        self.assertEqual(se.week_and_day(epoch(2026, 11, 22, 21, 30)), ("2026-W47", "2026-11-22"))

    def test_summer_boundary_uses_utc_plus_3(self):
        # 21:30 UTC on Sunday 5 Jul 2026 is 00:30 Monday 6 Jul in Cairo (UTC+3)
        self.assertEqual(se.week_and_day(epoch(2026, 7, 5, 21, 30)), ("2026-W28", "2026-07-06"))
        self.assertEqual(se.week_and_day(epoch(2026, 7, 5, 20, 30)), ("2026-W27", "2026-07-05"))

    def test_day_start_in_winter_summer_and_on_dst_change_days(self):
        noon = lambda y, m, d: epoch(y, m, d, 12, tz=CAIRO)
        self.assertEqual(ta.day_start(noon(2026, 11, 23)), epoch(2026, 11, 22, 22))
        self.assertEqual(ta.day_start(noon(2026, 7, 6)), epoch(2026, 7, 5, 21))
        self.assertEqual(ta.day_start(noon(2026, 4, 24)), epoch(2026, 4, 23, 22))   # clocks jump 00:00 -> 01:00
        self.assertEqual(ta.day_start(noon(2026, 10, 29)), epoch(2026, 10, 28, 21))  # clocks go back 24:00 -> 23:00

    def test_closer_closes_the_week_that_just_ended(self):
        at = lambda *a: datetime.datetime(*a, tzinfo=CAIRO)
        self.assertEqual(wc.closing_week(at(2026, 11, 23, 0, 5)), "2026-W47")   # Monday 00:05, winter
        self.assertEqual(wc.closing_week(at(2026, 7, 6, 0, 5)), "2026-W27")     # Monday 00:05, summer
        self.assertEqual(wc.closing_week(at(2027, 1, 4, 0, 5)), "2026-W53")     # across the year boundary

    def test_last_minute_sunday_task_lands_in_the_week_the_closer_ranks(self):
        done = epoch(2026, 11, 22, 23, 59, 30, tz=CAIRO)    # 23:59:30 Sunday in Cairo
        week, _ = se.week_and_day(done)
        closer = wc.closing_week(datetime.datetime(2026, 11, 23, 0, 5, tzinfo=CAIRO))
        self.assertEqual(week, closer)

    def test_leaderboard_default_week_is_cairo(self):
        year, week, _ = datetime.datetime.now(CAIRO).isocalendar()
        self.assertEqual(lb.current_week(), f"{year}-W{week:02d}")


class EstimateLockTests(unittest.TestCase):
    class Table:
        def __init__(self, status):
            self.status, self.updated = status, False

        def update_item(self, Key, ConditionExpression, ExpressionAttributeValues, **kw):
            allowed = ExpressionAttributeValues.get(":todo")
            if allowed and self.status != allowed:
                raise ClientError("ConditionalCheckFailedException")
            self.updated = True

    def _put(self, status):
        ta.table = self.Table(status)
        event = {"requestContext": {"authorizer": {"claims": {"sub": "u1"}}},
                 "httpMethod": "PUT", "resource": "/tasks/{id}", "pathParameters": {"id": "t1"},
                 "body": '{"title": "Write report", "estimateMin": 30}'}
        return ta.lambda_handler(event, None)["statusCode"], ta.table.updated

    def test_estimate_can_change_before_start_only(self):
        self.assertEqual(self._put("todo"), (200, True))
        self.assertEqual(self._put("in_progress"), (409, False))
        self.assertEqual(self._put("done"), (409, False))


if __name__ == "__main__":
    unittest.main()
