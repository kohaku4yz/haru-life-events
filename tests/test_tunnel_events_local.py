from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from cryptography.fernet import Fernet
from haru_life_events.tunnel_events_local import build_single_owner_app

class LocalEntryPointTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.root=root
        files=[
            ("api",b"a"*40),
            ("fernet",Fernet.generate_key()),
            ("identity",b"i"*40),
        ]
        for name,val in files:
            file=root/name
            file.write_bytes(val)
            file.chmod(0o600)
        self.env={
            "HARU_EVENTS_BRIDGE_ENABLED":"1",
            "HARU_EVENTS_OWNER":"owner-cat",
            "HARU_EVENTS_API_KEY_FILE":str(root/"api"),
            "HARU_EVENTS_FERNET_KEY_FILE":str(root/"fernet"),
            "HARU_EVENTS_ID_KEY_FILE":str(root/"identity"),
            "HARU_EVENTS_SUBSCRIPTIONS_DB":str(root/"subscriptions.sqlite"),
        }

    def test_disabled_by_default(self):
        with self.assertRaises(RuntimeError):
            build_single_owner_app(settings={})
        self.assertFalse((self.root/"subscriptions.sqlite").exists())

    def test_configured_private_app_only(self):
        app=build_single_owner_app(settings=self.env)
        self.assertEqual(app.authenticate({"authorization":"Bearer "+"a"*40}),"owner-cat")
        self.assertIsNone(app.authenticate({"authorization":"Bearer "+"b"*40}))

    def test_insecure_secret_file_rejected(self):
        (self.root/"api").chmod(0o644)
        with self.assertRaises(ValueError):
            build_single_owner_app(settings=self.env)

    def test_no_database_directory_rejected(self):
        self.env["HARU_EVENTS_SUBSCRIPTIONS_DB"]=str(self.root/"missing"/"subs.sqlite")
        with self.assertRaises(ValueError):
            build_single_owner_app(settings=self.env)

if __name__=="__main__":
    unittest.main()
