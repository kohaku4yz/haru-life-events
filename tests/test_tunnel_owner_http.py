"""HTTP wire contract through ASGI, with real fixed bearer and synthetic callback."""
from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from haru_life_events.event_bridge_v2 import EventASGI, EventRpc, EventSubscriptions
from haru_life_events.tunnel_owner_auth import SingleOwnerTunnelAuth

TOKEN="t"*48
KEY=b"k"*32
MCP_VERSION="2026-07-28"
META={"io.modelcontextprotocol/protocolVersion": MCP_VERSION,
      "io.modelcontextprotocol/clientCapabilities":{}}

class NoNetworkChallenge:
    def __init__(self):self.calls=0
    def verify(self, url, secret, sid, now):
        self.calls+=1
        assert secret==KEY
        assert url=="https://events.example.org/owner"

class HTTPTunnelTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.challenge=NoNetworkChallenge()
        self.store=EventSubscriptions(Path(self.temp.name)/"subs.sqlite",
            encryption_key=Fernet.generate_key(),identity_key=b"i"*32,
            verifier=self.challenge)
        self.server=EventASGI(EventRpc(self.store),
            authenticate=SingleOwnerTunnelAuth("owner-cat",TOKEN.encode()))

    def request(self, method,params=None,token=TOKEN,header_method=None,
                protocol=MCP_VERSION,headers_extra=None,with_auth=True,
                with_meta=True):
        body={"jsonrpc":"2.0","id":1,"method":method,
              "params":{**(params or {}),**({"_meta": META} if with_meta else {})}}
        headers=[("content-type","application/json"),
                 ("mcp-protocol-version",protocol),
                 ("mcp-method",header_method or method)]
        if with_auth: headers.append(("authorization","Bearer "+token))
        if headers_extra:headers.extend(headers_extra)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server),
                                         base_url="http://testserver") as client:
                return await client.post("/mcp",json=body,headers=headers)
        return asyncio.run(run())

    def test_discover_events_list_no_oauth_with_fixed_bearer(self):
        resp=self.request("server/discover")
        self.assertEqual(resp.status_code,200)
        self.assertEqual(resp.json()["result"]["supportedVersions"],[MCP_VERSION])
        resp=self.request("events/list")
        self.assertEqual(resp.status_code,200)
        self.assertIn("life.location.changed",
                      [x["name"] for x in resp.json()["result"]["events"]])

    def test_subscribe_owner_scoped_refresh_then_unsubscribe(self):
        params={"name":"life.location.changed","arguments":{},
            "delivery":{"mode":"webhook","url":"https://events.example.org/owner",
                        "secret":"whsec_"+base64.b64encode(KEY).decode()}}
        resp=self.request("events/subscribe",params)
        self.assertEqual(resp.status_code,200)
        sid=resp.json()["result"]["id"]
        self.assertEqual(self.request("events/subscribe",params).json()["result"]["id"],sid)
        self.assertEqual(len(self.store.eligible("owner-cat","life.location.changed",
                                                datetime.now(timezone.utc))),1)
        self.assertEqual(self.challenge.calls,2)
        deleted=self.request("events/unsubscribe",{
            "name":params["name"],"arguments":params["arguments"],
            "delivery":{"mode":"webhook","url":params["delivery"]["url"]}})
        self.assertEqual(deleted.json()["result"],{})

    def test_no_credentials_rejected(self):
        self.assertEqual(self.request("events/list",with_auth=False).status_code,401)

    def test_wrong_credential_rejected(self):
        self.assertEqual(self.request("events/list",token="wrong"*10).status_code,401)

    def test_spoofed_identity_header_is_ignored(self):
        resp=self.request("events/list",headers_extra=[("x-user-id","another"),
            ("x-forwarded-user","another")])
        self.assertEqual(resp.status_code,200)
        self.assertEqual(self.store.eligible("another","life.location.changed",
                                            datetime.now(timezone.utc)),[])

    def test_duplicate_identity_header_rejected(self):
        self.assertEqual(self.request("events/list",headers_extra=[
            ("authorization","Bearer "+"t"*48)]).status_code,401)

    def test_duplicate_route_header_rejected(self):
        self.assertEqual(self.request("events/list",headers_extra=[
            ("mcp-method","events/list")]).status_code,401)

    def test_incorrect_route_header_rejected(self):
        resp=self.request("events/list",header_method="events/subscribe")
        self.assertEqual(resp.status_code,400)

    def test_legacy_protocol_rejected(self):
        self.assertEqual(self.request("events/list",protocol="2025-11-25").status_code,400)

    def test_missing_meta_rejected(self):
        self.assertEqual(self.request("events/list",with_meta=False).status_code,400)

    def test_auth_gate_disabled_by_default(self):
        default=EventASGI(EventRpc(self.store))
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=default),
                                         base_url="http://testserver") as client:
                return await client.post("/mcp",json={"jsonrpc":"2.0","method":"events/list","id":1})
        self.assertEqual(asyncio.run(run()).status_code,401)

if __name__=="__main__":
    unittest.main()
