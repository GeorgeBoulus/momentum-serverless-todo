"""leaderboard-api: read-only endpoints for the weekly leaderboard, rewards and awards.

GET /leaderboard    top 5 for a week      (cached 60 s at API Gateway; same data for everyone)
GET /leaderboard/me the caller's rank     (not cached; per-user)
GET /rewards        reward catalog        (cached 300 s)
GET /awards/me      the caller's awards   (not cached; per-user)

Reference implementation (design documentation; not deployed).
Environment variables: SCORES_TABLE, AWARDS_TABLE, CATALOG_TABLE, ALLOWED_ORIGIN, APP_TIMEZONE
"""
import datetime
import json
import os
import re
from decimal import Decimal
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key

db = boto3.resource("dynamodb")
scores = db.Table(os.environ["SCORES_TABLE"])
awards = db.Table(os.environ["AWARDS_TABLE"])
catalog = db.Table(os.environ["CATALOG_TABLE"])
INDEX = "weekId-points-index"
TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Africa/Cairo"))   # same timezone as scoring-engine
HEADERS = {"Content-Type": "application/json",
           "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*")}


def _num(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError


def respond(code, body):
    return {"statusCode": code, "headers": HEADERS, "body": json.dumps(body, default=_num)}


def current_week():
    year, week, _ = datetime.datetime.now(TZ).isocalendar()
    return f"{year}-W{week:02d}"


def top5(week):
    res = scores.query(IndexName=INDEX, KeyConditionExpression=Key("weekId").eq(week),
                       ScanIndexForward=False, Limit=5)
    return [{"rank": i + 1, "displayName": it["displayName"], "points": it["points"],
             "tasksCompleted": it.get("tasksCompleted", 0)} for i, it in enumerate(res["Items"])]


def my_rank(user, week):
    me = scores.get_item(Key={"weekId": week, "userId": user}).get("Item")
    if not me:
        return {"week": week, "points": 0, "rank": None}
    ahead = scores.query(IndexName=INDEX, Select="COUNT",
                         KeyConditionExpression=Key("weekId").eq(week) & Key("points").gt(me["points"]))["Count"]
    return {"week": week, "points": me["points"], "rank": ahead + 1}


def lambda_handler(event, context):
    claims = event["requestContext"]["authorizer"]["claims"]
    resource = event["resource"]
    week = (event.get("queryStringParameters") or {}).get("week") or current_week()
    if not re.fullmatch(r"\d{4}-W\d{2}", week):
        return respond(400, {"message": "week must look like 2026-W47"})
    if resource == "/leaderboard":
        return respond(200, {"week": week, "top": top5(week)})
    if resource == "/leaderboard/me":
        return respond(200, my_rank(claims["sub"], week))
    if resource == "/rewards":
        return respond(200, sorted(catalog.scan()["Items"], key=lambda i: i["rank"]))
    if resource == "/awards/me":
        res = awards.query(KeyConditionExpression=Key("userId").eq(claims["sub"]),
                           ScanIndexForward=False, Limit=20)
        return respond(200, res["Items"])
    return respond(404, {"message": "Not found"})
