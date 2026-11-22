"""scoring-engine: DynamoDB Streams consumer that turns task events into points.

Idempotent by design: it computes the TOTAL points a task deserves and awards only the difference
from `pointsAwarded`, inside one DynamoDB transaction. Re-delivered stream records add nothing.
The event source mapping uses a filter (status = done), and a second invocation caused by this
function's own write finds a delta of 0, so it cannot loop.

Reference implementation (design documentation; not deployed).
Environment variables: TASKS_TABLE, SCORES_TABLE, BASE_POINTS, PROOF_BONUS, DAILY_BONUS
"""
import datetime
import os
import time

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

ddb = boto3.client("dynamodb")
deser = TypeDeserializer()
TASKS = os.environ["TASKS_TABLE"]
SCORES = os.environ["SCORES_TABLE"]
BASE = int(os.environ.get("BASE_POINTS", "10"))
PROOF_BONUS = int(os.environ.get("PROOF_BONUS", "5"))
DAILY_BONUS = int(os.environ.get("DAILY_BONUS", "3"))


def week_and_day(ts):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}", d.date().isoformat()


def accuracy_bonus(estimate, actual):
    if not estimate:
        return 0
    ratio = actual / estimate
    if 0.8 <= ratio <= 1.2:
        return 5
    if 0.5 <= ratio <= 1.5:
        return 2
    return 0


def total_points(task):
    if task.get("status") != "done" or not task.get("scoreEligible", False):
        return 0
    pts = BASE + accuracy_bonus(float(task.get("estimateMin", 0)), float(task.get("actualMin", 0)))
    if task.get("proofStatus") == "approved":
        pts += PROOF_BONUS
    return pts


def award(task, total, delta):
    user, task_id = task["userId"], task["taskId"]
    prev = int(task.get("pointsAwarded", 0))
    first = prev == 0                       # only the first award for a task can earn the daily bonus
    week, day = week_and_day(int(task["completedAt"]))
    for with_bonus in ([True, False] if first else [False]):
        items = [
            {"Update": {                    # mark the task as scored (idempotency guard)
                "TableName": TASKS, "Key": {"userId": {"S": user}, "taskId": {"S": task_id}},
                "UpdateExpression": "SET pointsAwarded = :t",
                "ConditionExpression": "attribute_not_exists(pointsAwarded) OR pointsAwarded = :p",
                "ExpressionAttributeValues": {":t": {"N": str(total)}, ":p": {"N": str(prev)}}}},
            {"Update": {                    # atomic counter on the weekly score
                "TableName": SCORES, "Key": {"weekId": {"S": week}, "userId": {"S": user}},
                "UpdateExpression": "SET displayName = :n ADD points :d, tasksCompleted :c",
                "ExpressionAttributeValues": {
                    ":n": {"S": task.get("displayName", "Player")},
                    ":d": {"N": str(delta + (DAILY_BONUS if with_bonus else 0))},
                    ":c": {"N": "1" if first else "0"}}}},
        ]
        if with_bonus:                      # one marker per user per day = "first scored task of the day"
            items.append({"Put": {
                "TableName": SCORES, "ConditionExpression": "attribute_not_exists(weekId)",
                "Item": {"weekId": {"S": week}, "userId": {"S": f"DAY#{user}#{day}"},
                         "expiresAt": {"N": str(int(time.time()) + 30 * 86400)}}}})
        try:
            ddb.transact_write_items(TransactItems=items)
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = e.response.get("CancellationReasons", [])
            if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                return                      # another invocation already scored this task
            if with_bonus:
                continue                    # daily bonus already given today; retry without it
            raise


def lambda_handler(event, context):
    for rec in event["Records"]:
        if rec["eventName"] not in ("INSERT", "MODIFY"):
            continue
        task = {k: deser.deserialize(v) for k, v in rec["dynamodb"]["NewImage"].items()}
        total = total_points(task)
        delta = total - int(task.get("pointsAwarded", 0))
        if delta > 0:
            award(task, total, delta)
