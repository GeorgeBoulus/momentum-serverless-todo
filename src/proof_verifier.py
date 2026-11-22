"""proof-verifier: consumes S3 upload events from SQS and decides whether a proof photo is accepted.

Checks, in order: duplicate photo (SHA-256 conditional write), unsafe content (Rekognition
moderation), and "is this a real photo" (Rekognition labels). Then updates the task.
Uses SQS partial batch responses so one bad message does not block the others.

Reference implementation (design documentation; not deployed).
Environment variables: TASKS_TABLE, HASHES_TABLE
"""
import hashlib
import json
import os
import time
import urllib.parse

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")
rek = boto3.client("rekognition")
ddb = boto3.client("dynamodb")
TASKS = os.environ["TASKS_TABLE"]
HASHES = os.environ["HASHES_TABLE"]
HASH_TTL_SECONDS = 90 * 86400


def verify(bucket, key):
    parts = key.split("/")
    if len(parts) != 4 or parts[0] != "proofs":
        print("Skipping unexpected key", key)
        return
    _, user, task_id, _ = parts
    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    digest = hashlib.sha256(data).hexdigest()
    status, reason, labels = "approved", None, []

    try:   # 1) duplicate photo anywhere in the system
        ddb.put_item(TableName=HASHES, ConditionExpression="attribute_not_exists(sha256)",
                     Item={"sha256": {"S": digest}, "userId": {"S": user}, "taskId": {"S": task_id},
                           "expiresAt": {"N": str(int(time.time()) + HASH_TTL_SECONDS)}})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        status, reason = "rejected", "duplicate"

    if status == "approved":   # 2) unsafe content
        if rek.detect_moderation_labels(Image={"Bytes": data}, MinConfidence=70)["ModerationLabels"]:
            status, reason = "rejected", "unsafe_content"

    if status == "approved":   # 3) must look like a real photo of something
        labels = [l["Name"] for l in rek.detect_labels(
            Image={"Bytes": data}, MaxLabels=8, MinConfidence=75)["Labels"]]
        if not labels:
            status, reason = "rejected", "unrecognizable"

    names = {"#p": "proofStatus"}
    values = {":s": {"S": status}}
    expr = "SET #p = :s"
    if status == "approved":
        expr += ", proofKey = :k, proofLabels = :l"
        values[":k"] = {"S": key}
        values[":l"] = {"SS": labels} if labels else {"SS": ["photo"]}
    else:
        expr += ", proofReason = :r"
        values[":r"] = {"S": reason}
    try:
        ddb.update_item(TableName=TASKS, Key={"userId": {"S": user}, "taskId": {"S": task_id}},
                        UpdateExpression=expr, ExpressionAttributeNames=names,
                        ExpressionAttributeValues=values, ConditionExpression="attribute_exists(taskId)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        status = "rejected"   # task was deleted meanwhile
    if status == "rejected":
        s3.delete_object(Bucket=bucket, Key=key)   # privacy and cost: do not keep rejected photos


def lambda_handler(event, context):
    failures = []
    for rec in event["Records"]:
        try:
            for r in json.loads(rec["body"]).get("Records", []):
                verify(r["s3"]["bucket"]["name"], urllib.parse.unquote_plus(r["s3"]["object"]["key"]))
        except Exception as e:   # SQS retries this message, then moves it to the DLQ
            print("ERROR", rec["messageId"], e)
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}
