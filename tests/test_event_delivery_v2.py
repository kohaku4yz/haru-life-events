"""Offline delivery retry and safety tests with an injected fake HTTP client."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography.fernet import Fernet
from haru_life_events.event_bridge_v2 import CallbackError, EventSubscriptions
from haru_life_events.event_delivery_v2 import EventDeliveryQueue, validate_event

NOW=datetime(2026,10,11,13,tzinfo=timezone.utc)
import base64
KEY=base64.b64encode(b"k"*32).decode()
NAME="life.location.changed"
URL="https://events.example.org/dispatch"
def event(eid="evt_12345678"):
    return {"eventId":eid,"name":NAME,"timestamp":"2026-10-11T13:00:00Z",
            "data":{"place":"千代田区, 東京都","observed_at":"2026-10-11T13:00:00Z","reason":"sharing_started"},"cursor":None}

class Verified:
    def verify(self,url,secret,sid,now): pass

class FakeSender:
    def __init__(self,codes):
        self.codes=list(codes)
        self.calls=[]
    def deliver(self,**kwargs):
        self.calls.append(kwargs)
        code=self.codes.pop(0)
        if isinstance(code,Exception): raise code
        return code

class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)
        self.store=EventSubscriptions(path/"subs.sqlite", encryption_key=Fernet.generate_key(),
                                      identity_key=b"q"*32,verifier=Verified())
        self.outbox=EventDeliveryQueue(path/"outbox.sqlite",self.store)
        self.store.subscribe("cat",{"name":NAME,"arguments":{},
            "delivery":{"mode":"webhook","url":URL,"secret":"whsec_"+KEY}},NOW)

    def test_queue_dedup_and_delivery(self):
        self.assertEqual(self.outbox.enqueue(owner="cat",event=event(),now=NOW),1)
        self.assertEqual(self.outbox.enqueue(owner="cat",event=event(),now=NOW),0)
        sender=FakeSender([202])
        self.assertEqual(self.outbox.drain_one(owner="cat",now=NOW,sender=sender),"sent")
        self.assertEqual(self.outbox.drain_one(owner="cat",now=NOW,sender=sender),"empty")
        self.assertEqual(self.outbox.counts("cat"),{"sent":1})
        self.assertEqual(sender.calls[0]["event"]["data"],event()["data"])
        self.assertEqual(sender.calls[0]["secret"],b"k"*32)

    def test_offline_default_cannot_send(self):
        self.outbox.enqueue(owner="cat",event=event(),now=NOW)
        self.assertEqual(self.outbox.drain_one(owner="cat",now=NOW),"disabled")
        self.assertEqual(self.outbox.counts("cat"),{"pending":1})

    def test_failed_delivery_schedules_backoff_and_retries(self):
        self.outbox.enqueue(owner="cat",event=event(),now=NOW)
        sender=FakeSender([TimeoutError(),202])
        self.assertEqual(self.outbox.drain_one(owner="cat",now=NOW,sender=sender),"retry")
        self.assertEqual(self.outbox.drain_one(owner="cat",now=NOW+timedelta(seconds=5),sender=sender),"empty")
        self.assertEqual(self.outbox.drain_one(owner="cat",now=NOW+timedelta(seconds=31),sender=sender),"sent")
        self.assertEqual(len(sender.calls),2)
        self.assertEqual(sender.calls[0]["event"]["eventId"],sender.calls[1]["event"]["eventId"])

    def test_terminal_410_and_413(self):
        for code in (410,413):
            with self.subTest(code=code):
                self.outbox.enqueue(owner="cat",event=event(f"evt_term_{code}"),now=NOW)
                sender=FakeSender([code])
                self.assertEqual(self.outbox.drain_one(owner="cat",now=NOW,sender=sender),"permanent")

    def test_revoked_sub_cannot_receive(self):
        self.outbox.enqueue(owner="cat",event=event(),now=NOW)
        self.store.unsubscribe("cat",{"name":NAME,"arguments":{},
                   "delivery":{"mode":"webhook","url":URL}})
        fake=FakeSender([200])
        self.assertEqual(self.outbox.drain_one(owner="cat",now=NOW,sender=fake),"permanent")
        self.assertEqual(fake.calls,[])

    def test_does_not_cross_owner_boundary(self):
        self.outbox.enqueue(owner="cat",event=event(),now=NOW)
        sender=FakeSender([200])
        self.assertEqual(self.outbox.drain_one(owner="other",now=NOW,sender=sender),"empty")
        self.assertEqual(sender.calls,[])

    def test_rejects_sensitive_fields(self):
        broken=event()
        broken["data"]={"zone":"home","change":"arrived","latitude":"35"}
        with self.assertRaises(ValueError):
            validate_event(broken)
        broken2=event()
        broken2["cursor"]="secret"
        with self.assertRaises(ValueError):
            self.outbox.enqueue(owner="cat",event=broken2,now=NOW)

    def test_sender_failure_exhausted_after_six_attempts(self):
        self.outbox.enqueue(owner="cat",event=event(),now=NOW)
        sender=FakeSender([CallbackError("timeout")]*6)
        now=NOW
        for _ in range(5):
            self.assertEqual(self.outbox.drain_one(owner="cat",now=now,sender=sender),"retry")
            now+=timedelta(minutes=20)  # past every backoff (max 480s here), inside the 3h age cap
        self.assertEqual(self.outbox.drain_one(owner="cat",now=now,sender=sender),"exhausted")
        self.assertEqual(self.outbox.counts("cat"),{"terminal":1})

if __name__=="__main__":
    unittest.main()
