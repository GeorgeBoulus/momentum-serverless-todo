import types
import unittest

import stubs  # noqa: F401  (installs boto3 stubs)
from stubs import ClientError
import proof_verifier as pv

KEY = "proofs/u1/t1/abc.jpg"


class FakeDDB:
    def __init__(self):
        self.hashes, self.updates = {}, []
        self.task = {"taskId": {"S": "t1"}}   # None means the task was deleted

    def put_item(self, TableName, ConditionExpression, Item):
        h = Item["sha256"]["S"]
        if h in self.hashes:
            raise ClientError("ConditionalCheckFailedException")
        self.hashes[h] = Item

    def get_item(self, TableName, Key, ConsistentRead=False):
        if "taskId" in Key:
            return {"Item": self.task} if self.task else {}
        item = self.hashes.get(Key["sha256"]["S"])
        return {"Item": item} if item else {}

    fail_update_once = False

    def update_item(self, **k):
        cond = k.get("ConditionExpression", "")
        pk = (self.task or {}).get("proofKey", {}).get("S")
        if "proofKey = :k0" in cond and pk and pk != k["ExpressionAttributeValues"][":k0"]["S"]:
            raise ClientError("ConditionalCheckFailedException")
        if self.fail_update_once:
            self.fail_update_once = False
            raise RuntimeError("update failed after hash write")
        self.updates.append(k["ExpressionAttributeValues"])


class FakeRek:
    def __init__(self, fail_first=False, unsafe=False):
        self.fail, self.unsafe = fail_first, unsafe

    def detect_moderation_labels(self, **k):
        if self.fail:
            self.fail = False
            raise RuntimeError("Throttling")
        return {"ModerationLabels": [{"Name": "x"}] if self.unsafe else []}

    def detect_labels(self, **k):
        return {"Labels": [{"Name": "Cat"}]}


class FakeS3:
    def __init__(self, data=b"photo"):
        self.data, self.deleted = data, []

    def get_object(self, Bucket, Key):
        return {"Body": types.SimpleNamespace(read=lambda: self.data)}

    def delete_object(self, **k):
        self.deleted.append(k)


class ProofVerifierTests(unittest.TestCase):
    def setUp(self):
        self.ddb, self.s3 = FakeDDB(), FakeS3()
        pv.ddb, pv.s3 = self.ddb, self.s3

    def test_retry_after_transient_error_is_approved(self):
        pv.rek = FakeRek(fail_first=True)
        with self.assertRaises(RuntimeError):
            pv.verify("b", KEY)
        pv.verify("b", KEY)  # SQS redelivery
        self.assertEqual(self.ddb.updates[-1][":s"]["S"], "approved")
        self.assertEqual(self.s3.deleted, [])

    def test_same_message_redelivered_after_success_stays_approved(self):
        pv.rek = FakeRek()
        pv.verify("b", KEY)
        pv.verify("b", KEY)
        self.assertEqual(self.ddb.updates[-1][":s"]["S"], "approved")

    def test_same_photo_under_another_task_is_duplicate(self):
        pv.rek = FakeRek()
        pv.verify("b", KEY)
        pv.verify("b", "proofs/u2/t9/zzz.jpg")
        self.assertEqual(self.ddb.updates[-1][":s"]["S"], "rejected")
        self.assertEqual(self.ddb.updates[-1][":r"]["S"], "duplicate")
        self.assertEqual(len(self.s3.deleted), 1)

    def test_redelivery_after_hash_write_but_before_task_update(self):
        pv.rek = FakeRek()
        self.ddb.fail_update_once = True
        with self.assertRaises(RuntimeError):
            pv.verify("b", KEY)
        self.assertEqual(len(self.ddb.hashes), 1)   # hash was written, task not updated
        pv.verify("b", KEY)
        self.assertEqual(self.ddb.updates[-1][":s"]["S"], "approved")

    def test_legacy_hash_row_without_photokey(self):
        pv.rek = FakeRek()
        import hashlib
        h = hashlib.sha256(b"photo").hexdigest()
        self.ddb.hashes[h] = {"sha256": {"S": h}, "userId": {"S": "u1"}, "taskId": {"S": "t1"}}
        pv.verify("b", KEY)                                  # same user+task: redelivery
        self.assertEqual(self.ddb.updates[-1][":s"]["S"], "approved")
        pv.verify("b", "proofs/u2/t9/zzz.jpg")               # other user: real duplicate
        self.assertEqual(self.ddb.updates[-1][":r"]["S"], "duplicate")

    def test_unsafe_photo_rejected_and_hash_not_burned(self):
        pv.rek = FakeRek(unsafe=True)
        pv.verify("b", KEY)
        self.assertEqual(self.ddb.updates[-1][":r"]["S"], "unsafe_content")
        self.assertEqual(self.ddb.hashes, {})


    def test_second_photo_does_not_replace_approved_proof(self):
        pv.rek = FakeRek()
        self.ddb.task = {"taskId": {"S": "t1"}, "proofKey": {"S": "proofs/u1/t1/first.jpg"}}
        pv.verify("b", KEY)
        self.assertEqual(self.ddb.updates, [])
        self.assertEqual(self.s3.deleted, [{"Bucket": "b", "Key": KEY}])

    def test_same_key_redelivery_with_existing_proof_is_kept(self):
        pv.rek = FakeRek()
        self.ddb.task = {"taskId": {"S": "t1"}, "proofKey": {"S": KEY}}
        pv.verify("b", KEY)
        self.assertEqual(self.ddb.updates[-1][":s"]["S"], "approved")
        self.assertEqual(self.s3.deleted, [])

    def test_photo_for_deleted_task_is_dropped(self):
        pv.rek = FakeRek()
        self.ddb.task = None
        pv.verify("b", KEY)
        self.assertEqual(self.s3.deleted, [{"Bucket": "b", "Key": KEY}])


if __name__ == "__main__":
    unittest.main()
