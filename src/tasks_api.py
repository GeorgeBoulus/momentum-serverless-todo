"""tasks-api: CRUD plus the start and complete actions for a user's tasks.

Reference implementation (design documentation; not deployed).
Runs behind API Gateway (REST, Lambda proxy) with a Cognito authorizer.
The user's identity ALWAYS comes from the validated JWT claim "sub".
Environment variables: TASKS_TABLE, MIN_ACTIVE_MIN, DAILY_SCORED_CAP, ALLOWED_ORIGIN
"""
import json
import os
import time
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

table = boto3.resource("dynamodb").Table(os.environ["TASKS_TABLE"])
MIN_ACTIVE_MIN = int(os.environ.get("MIN_ACTIVE_MIN", "2"))    # anti-farming: minimum real work time
DAILY_CAP = int(os.environ.get("DAILY_SCORED_CAP", "10"))      # anti-farming: scored tasks per UTC day
HEADERS = {"Content-Type": "application/json",
           "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*")}


def _num(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Cannot serialize {type(o)}")


def respond(code, body):
    return {"statusCode": code, "headers": HEADERS, "body": json.dumps(body, default=_num)}


def _validated(body):
    title = str(body.get("title", "")).strip()
    try:
        estimate = int(body.get("estimateMin", 0))
    except (TypeError, ValueError):
        raise ValueError("estimateMin must be a whole number")
    if not 1 <= len(title) <= 120:
        raise ValueError("title must be 1-120 characters")
    if not 1 <= estimate <= 480:
        raise ValueError("estimateMin must be between 1 and 480 minutes")
    return title, estimate


def list_tasks(user):
    res = table.query(KeyConditionExpression=Key("userId").eq(user))
    return respond(200, res["Items"])


def create_task(user, name, body):
    title, estimate = _validated(body)
    item = {"userId": user, "taskId": str(uuid.uuid4()), "title": title,
            "estimateMin": estimate, "status": "todo", "proofStatus": "none",
            "displayName": name, "createdAt": int(time.time())}
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(taskId)")
    return respond(201, item)


def update_task(user, task_id, body):
    title, estimate = _validated(body)
    table.update_item(
        Key={"userId": user, "taskId": task_id},
        UpdateExpression="SET title = :t, estimateMin = :e",
        ConditionExpression="attribute_exists(taskId) AND #s <> :done",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":t": title, ":e": estimate, ":done": "done"})
    return respond(200, {"updated": task_id})


def delete_task(user, task_id):
    table.delete_item(Key={"userId": user, "taskId": task_id},
                      ConditionExpression="attribute_exists(taskId) AND #s <> :done",
                      ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":done": "done"})   # completed tasks count toward the daily cap
    return respond(200, {"deleted": task_id})


def start_task(user, task_id):
    table.update_item(
        Key={"userId": user, "taskId": task_id},
        UpdateExpression="SET #s = :ip, startedAt = :now",
        ConditionExpression="#s = :todo",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":ip": "in_progress", ":todo": "todo", ":now": int(time.time())})
    return respond(200, {"started": task_id})


def complete_task(user, task_id):
    task = table.get_item(Key={"userId": user, "taskId": task_id}).get("Item")
    if not task or task["status"] != "in_progress":
        return respond(409, {"message": "Task must be in progress"})
    now = int(time.time())
    actual = max(1, round((now - int(task["startedAt"])) / 60))
    midnight = now - now % 86400
    done_today = table.query(
        IndexName="userId-completedAt-index",
        KeyConditionExpression=Key("userId").eq(user) & Key("completedAt").gte(midnight),
        Select="COUNT")["Count"]
    eligible = actual >= MIN_ACTIVE_MIN and done_today < DAILY_CAP
    table.update_item(
        Key={"userId": user, "taskId": task_id},
        UpdateExpression="SET #s = :done, completedAt = :now, actualMin = :a, scoreEligible = :el",
        ConditionExpression="#s = :ip",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":done": "done", ":ip": "in_progress", ":now": now,
                                   ":a": actual, ":el": eligible})
    return respond(200, {"taskId": task_id, "actualMin": actual, "scoreEligible": eligible})


def lambda_handler(event, context):
    claims = event["requestContext"]["authorizer"]["claims"]
    user = claims["sub"]
    name = claims.get("nickname") or "Player-" + user[:6]   # never expose the email on the leaderboard
    route = (event["httpMethod"], event["resource"])
    task_id = (event.get("pathParameters") or {}).get("id")
    try:
        body = json.loads(event.get("body") or "{}")
        if route == ("GET", "/tasks"):
            return list_tasks(user)
        if route == ("POST", "/tasks"):
            return create_task(user, name, body)
        if route == ("PUT", "/tasks/{id}"):
            return update_task(user, task_id, body)
        if route == ("DELETE", "/tasks/{id}"):
            return delete_task(user, task_id)
        if route == ("POST", "/tasks/{id}/start"):
            return start_task(user, task_id)
        if route == ("POST", "/tasks/{id}/complete"):
            return complete_task(user, task_id)
        return respond(404, {"message": "Not found"})
    except ValueError as e:
        return respond(400, {"message": str(e)})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return respond(409, {"message": "Task is not in the right state for this action"})
        print("ERROR", e)
        return respond(500, {"message": "Server error"})
    except Exception as e:  # log details, return a generic message
        print("ERROR", e)
        return respond(500, {"message": "Server error"})
