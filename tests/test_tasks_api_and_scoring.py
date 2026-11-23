import os
import unittest

import stubs  # noqa: F401
from stubs import ClientError
import tasks_api as ta
import scoring_engine as se


class FakeTable:
    def __init__(self, status):
        self.item = {"status": status}

    def delete_item(self, Key, ConditionExpression, **kw):
        if "#s <> :done" in ConditionExpression and self.item["status"] == "done":
            raise ClientError("ConditionalCheckFailedException")
        self.item = None


class DeleteGuardTests(unittest.TestCase):
    def _run(self, status):
        ta.table = FakeTable(status)
        event = {"requestContext": {"authorizer": {"claims": {"sub": "u1"}}},
                 "httpMethod": "DELETE", "resource": "/tasks/{id}", "pathParameters": {"id": "t1"}}
        return ta.lambda_handler(event, None)["statusCode"]

    def test_cannot_delete_completed_task(self):
        self.assertEqual(self._run("done"), 409)

    def test_can_delete_open_task(self):
        self.assertEqual(self._run("todo"), 200)
        self.assertEqual(self._run("in_progress"), 200)


class ScoringTests(unittest.TestCase):
    def test_accuracy_bonus_bands(self):
        self.assertEqual(se.accuracy_bonus(10, 10), 5)
        self.assertEqual(se.accuracy_bonus(10, 14), 2)
        self.assertEqual(se.accuracy_bonus(10, 20), 0)
        self.assertEqual(se.accuracy_bonus(0, 5), 0)

    def test_total_points(self):
        t = {"status": "done", "scoreEligible": True, "estimateMin": 10, "actualMin": 10, "proofStatus": "approved"}
        self.assertEqual(se.total_points(t), 20)
        t["proofStatus"] = "none"
        self.assertEqual(se.total_points(t), 15)
        t["scoreEligible"] = False
        self.assertEqual(se.total_points(t), 0)


if __name__ == "__main__":
    unittest.main()
