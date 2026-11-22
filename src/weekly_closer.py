"""weekly-closer: invoked by EventBridge Scheduler (Sunday 23:59, Africa/Cairo).

Ranks the top 5 of the week, creates one award per winner (idempotent conditional write),
emails the winners through SES and posts a summary to the ops SNS topic.

Reference implementation (design documentation; not deployed).
Environment variables: SCORES_TABLE, AWARDS_TABLE, CATALOG_TABLE, USER_POOL_ID, SENDER_EMAIL, OPS_TOPIC_ARN
"""
import datetime
import os
import time

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


def current_week():
    year, week, _ = datetime.datetime.now(datetime.timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def email_of(sub):
    user = cognito.admin_get_user(UserPoolId=os.environ["USER_POOL_ID"], Username=sub)
    return next((a["Value"] for a in user["UserAttributes"] if a["Name"] == "email"), None)


def lambda_handler(event, context):
    week = (event or {}).get("weekId") or current_week()
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
