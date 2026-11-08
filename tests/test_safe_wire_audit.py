import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from cryptography.fernet import Fernet
import httpx
from haru_life_events.event_bridge_v2 import EventASGI, EventRpc, EventSubscriptions
from haru_life_events.wire_audit import SafeWireAudit

class AuditTests(unittest.TestCase):
    def test_records_only_method_and_failure_class(self):
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td)/"private"
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)
            log=parent/"mcp-log.jsonl"
            audit=SafeWireAudit(log)
            store=EventSubscriptions(parent/"subs.sqlite",
                encryption_key=Fernet.generate_key(), identity_key=b"i"*40)
            app=EventASGI(EventRpc(store), authenticate=lambda headers:"canary", audit=audit)
            async def request(method, params):
                body={"jsonrpc":"2.0","id":1,"method":method,"params":params}
                headers={"content-type":"application/json","mcp-protocol-version":"2026-07-28","mcp-method":method}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://localhost") as client:
                    return await client.post("/mcp", json=body,headers=headers)
            valid={"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities":{}}}
            self.assertEqual(asyncio.run(request("events/list",valid)).status_code,200)
            self.assertEqual(asyncio.run(request("events/subscribe",{})).status_code,400)
            rows=[json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([(x["method"],x["result"]) for x in rows],
                [("events/list","rpc_success"),("events/subscribe","bad_protocol_meta")])
            self.assertEqual(log.stat().st_mode & 0o077,0)

    def test_refuses_insecure_directory(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"bad"
            path.mkdir(mode=0o755)
            path.chmod(0o755)
            with self.assertRaises(ValueError):
                SafeWireAudit(path/"log.jsonl")

if __name__=="__main__":
    unittest.main()
