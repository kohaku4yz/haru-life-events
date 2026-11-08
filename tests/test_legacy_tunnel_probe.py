"""Legacy initialize is permitted only for authenticated startup probing."""
import asyncio
import unittest
import httpx
from cryptography.fernet import Fernet
from tempfile import TemporaryDirectory
from pathlib import Path

from haru_life_events.event_bridge_v2 import EventASGI, EventRpc, EventSubscriptions
from haru_life_events.tunnel_owner_auth import SingleOwnerTunnelAuth


class LegacyProbeTests(unittest.TestCase):
    def test_only_authenticated_initialize_is_permitted_without_mcp2_headers(self):
        with TemporaryDirectory() as d:
            store=EventSubscriptions(Path(d)/"subs.db",
                                     encryption_key=Fernet.generate_key(),
                                     identity_key=b"i"*40)
            app=EventASGI(EventRpc(store),
                          authenticate=SingleOwnerTunnelAuth("cat",b"k"*40))
            async def post(payload,authenticated=True,method_header=None,protocol=None):
                headers={"content-type":"application/json"}
                if authenticated:headers["authorization"]="Bearer "+"k"*40
                if method_header:headers["mcp-method"]=method_header
                if protocol:headers["mcp-protocol-version"]=protocol
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as cli:
                    return await cli.post("/mcp",json=payload,headers=headers)
            message={"jsonrpc":"2.0","id":1,"method":"initialize","params":{
                "protocolVersion":"2025-06-18","capabilities":{},
                "clientInfo":{"name":"tunnel-probe","version":"0.0.11"}}}
            response=asyncio.run(post(message))
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()["result"]["protocolVersion"],"2025-06-18")
            self.assertEqual(asyncio.run(post(message,authenticated=False)).status_code,401)
            self.assertEqual(asyncio.run(post(message,protocol="2026-07-28")).status_code,400)
            other={"jsonrpc":"2.0","id":2,"method":"events/list","params":{}}
            self.assertEqual(asyncio.run(post(other)).status_code,400)
            self.assertEqual(asyncio.run(post({**message,"params":{"protocolVersion":"evil"}})).status_code,400)
            notification={"jsonrpc":"2.0","method":"notifications/initialized"}
            self.assertEqual(asyncio.run(post(notification)).status_code,202)

if __name__=="__main__":
    unittest.main()
