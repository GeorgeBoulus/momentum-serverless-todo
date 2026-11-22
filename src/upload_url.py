"""upload-url: issues a short-lived pre-signed POST so the browser uploads a proof photo
straight to the private S3 bucket (the photo never passes through API Gateway or Lambda).

Reference implementation (design documentation; not deployed).
Environment variables: TASKS_TABLE, PROOF_BUCKET, MAX_PHOTO_BYTES, ALLOWED_ORIGIN
"""
import json
import os
import uuid

import boto3

table = boto3.resource("dynamodb").Table(os.environ["TASKS_TABLE"])
s3 = boto3.client("s3")
BUCKET = os.environ["PROOF_BUCKET"]
MAX_BYTES = int(os.environ.get("MAX_PHOTO_BYTES", str(5 * 1024 * 1024)))  # Rekognition inline limit is 5 MB
HEADERS = {"Content-Type": "application/json",
           "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*")}


def respond(code, body):
    return {"statusCode": code, "headers": HEADERS, "body": json.dumps(body)}


def lambda_handler(event, context):
    user = event["requestContext"]["authorizer"]["claims"]["sub"]
    task_id = event["pathParameters"]["id"]
    task = table.get_item(Key={"userId": user, "taskId": task_id}).get("Item")
    if not task:
        return respond(404, {"message": "Task not found"})
    if task["status"] == "todo":
        return respond(409, {"message": "Start the task before adding proof"})
    if task.get("proofStatus") == "approved":
        return respond(409, {"message": "Proof already approved"})

    key = f"proofs/{user}/{task_id}/{uuid.uuid4()}.jpg"   # the key encodes owner and task
    post = s3.generate_presigned_post(
        Bucket=BUCKET, Key=key,
        Fields={"Content-Type": "image/jpeg"},
        Conditions=[{"Content-Type": "image/jpeg"},
                    ["content-length-range", 1024, MAX_BYTES]],   # size limit enforced by S3 itself
        ExpiresIn=300)                                             # URL valid for 5 minutes
    return respond(200, {"url": post["url"], "fields": post["fields"], "key": key,
                         "expiresInSeconds": 300})
