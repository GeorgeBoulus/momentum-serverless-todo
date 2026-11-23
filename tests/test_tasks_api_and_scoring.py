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


class BodyValidationTests(unittest.TestCase):
    def _post(self, body):
        ta.table = FakeTable("todo")
        event = {"requestContext": {"authorizer": {"claims": {"sub": "u1"}}},
                 "httpMethod": "POST", "resource": "/tasks", "body": body}
        return ta.lambda_handler(event, None)["statusCode"]

    def test_non_object_and_invalid_json_give_400(self):
        for body in ("[1, 2]", "42", "null", '"text"', "{not json"):
            self.assertEqual(self._post(body), 400, body)


class ListTasksTests(unittest.TestCase):
    def test_lists_newest_first_through_the_index_and_follows_pages(self):
        calls = []

        class T:
            def query(self, **kw):
                calls.append(kw)
                if "ExclusiveStartKey" not in kw:
                    return {"Items": [{"taskId": "new"}], "LastEvaluatedKey": {"k": 1}}
                return {"Items": [{"taskId": "old"}]}

        ta.table = T()
        res = ta.list_tasks("u1")
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(res["body"], '[{"taskId": "new"}, {"taskId": "old"}]')
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["IndexName"] == "userId-createdAt-index" and c["ScanIndexForward"] is False for c in calls))


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
