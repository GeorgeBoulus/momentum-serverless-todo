"""weekly-closer: invoked by EventBridge Scheduler (Monday 00:05, Africa/Cairo), just after the week it closes has ended.

Ranks the top 5 of the week, creates one award per winner (idempotent conditional write),
emails the winners through SES and posts a summary to the ops SNS topic.

Reference implementation (design documentation; not deployed).
Environment variables: SCORES_TABLE, AWARDS_TABLE, CATALOG_TABLE, USER_POOL_ID, SENDER_EMAIL, OPS_TOPIC_ARN, APP_TIMEZONE
"""
import datetime
import os
import time
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

db = boto3.resource("dynamodb")
scores = db.Table(os.environ["SCORES_TABLE"])
awards = db.Table(os.environ["AWARDS_TABLE"])
catalog = db.Table(os.environ["CATALOG_TABLE"])
cognito = boto3.client("cognito-idp")
ses = boto3.client("ses")
sns = boto3.client("sns")


TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Africa/Cairo"))   # same timezone as scoring-engine


def closing_week(now=None):
    """The week that just ended. The schedule fires at Monday 00:05 local time, so one hour earlier
    is still Sunday of the week being closed. An explicit weekId in the event overrides this."""
    now = now or datetime.datetime.now(TZ)
    year, week, _ = (now - datetime.timedelta(hours=1)).isocalendar()
    return f"{year}-W{week:02d}"


def email_of(sub):
    user = cognito.admin_get_user(UserPoolId=os.environ["USER_POOL_ID"], Username=sub)
    return next((a["Value"] for a in user["UserAttributes"] if a["Name"] == "email"), None)


def lambda_handler(event, context):
    week = (event or {}).get("weekId") or closing_week()
    winners = scores.query(IndexName="weekId-points-index", KeyConditionExpression=Key("weekId").eq(week),
                           ScanIndexForward=False, Limit=5)["Items"]
    summary = []
    for rank, w in enumerate(winners, start=1):
        reward = catalog.get_item(Key={"rank": rank}).get("Item", {"title": "Champion badge"})
        try:   # safe to re-run: an award is created only once per user and week
            awards.put_item(Item={"userId": w["userId"], "weekId": week, "rank": rank,
                                  "rewardTitle": reward["title"], "points": w["points"],
                                  "status": "issued", "createdAt": int(time.time())},
                            ConditionExpression="attribute_not_exists(weekId)")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue
            raise
        summary.append(f"#{rank} {w['displayName']} ({w['points']} pts): {reward['title']}")
        try:   # a failed email must never undo the award
            to = email_of(w["userId"])
            if to:
                ses.send_email(Source=os.environ["SENDER_EMAIL"], Destination={"ToAddresses": [to]},
                               Message={"Subject": {"Data": f"You finished #{rank} on Momentum this week!"},
                                        "Body": {"Text": {"Data": f"Congratulations! Your reward: {reward['title']}."}}})
        except Exception as e:
            print("EMAIL_FAILED", w["userId"], e)
    sns.publish(TopicArn=os.environ["OPS_TOPIC_ARN"], Subject=f"Momentum weekly close {week}",
                Message="\n".join(summary) or "No awards created.")
    return {"week": week, "awards": len(summary)}
