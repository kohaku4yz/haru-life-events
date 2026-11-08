"""Real MCP2 Events routing headers must match the event being subscribed."""
import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from cryptography.fernet import Fernet
import httpx
from haru_life_events.event_bridge_v2 import EventASGI, EventRpc, EventSubscriptions

class MockVerifier:
    def __init__(self): self.n=0
    def verify(self,url,secret,sid,now): self.n+=1

class HeaderTests(unittest.TestCase):
    def test_event_name_header_accept_reject_and_unsubscribe(self):
        with tempfile.TemporaryDirectory() as d:
            verify=MockVerifier()
            store=EventSubscriptions(Path(d)/'subs.db',
                encryption_key=Fernet.generate_key(),identity_key=b'I'*40,verifier=verify)
            logs=[]
            app=EventASGI(EventRpc(store),authenticate=lambda h:'cat',audit=lambda m,r:logs.append((m,r)))
            name='life.location.changed'
            params={'name':name,'arguments':{},
                'delivery':{'mode':'webhook','url':'https://example.org/hooks',
                    'secret':'whsec_'+base64.b64encode(b'X'*32).decode()}}
            meta={'io.modelcontextprotocol/protocolVersion':'2026-07-28',
                  'io.modelcontextprotocol/clientCapabilities':{}}
            async def send(method,body,name_header=None):
                headers={'content-type':'application/json','mcp-protocol-version':'2026-07-28','mcp-method':method}
                if name_header is not None:headers['mcp-name']=name_header
                request={'jsonrpc':'2.0','id':2,'method':method,
                         'params':dict(body,_meta=meta)}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testing') as c:
                    return await c.post('/mcp',json=request,headers=headers)
            wrong=asyncio.run(send('events/subscribe',params,'life.other'))
            self.assertEqual(wrong.status_code,400)
            self.assertEqual(verify.n,0)
            valid=asyncio.run(send('events/subscribe',params,name))
            self.assertEqual(valid.status_code,200)
            self.assertIn('result',valid.json())
            self.assertEqual(verify.n,1)
            self.assertEqual(len(store.eligible('cat',name,datetime.now(timezone.utc))),1)
            cancel=dict(params,delivery={'mode':'webhook','url':'https://example.org/hooks'})
            done=asyncio.run(send('events/unsubscribe',cancel,name))
            self.assertEqual(done.status_code,200)
            self.assertEqual(done.json().get('result'),{})
            self.assertEqual(len(store.eligible('cat',name,datetime.now(timezone.utc))),0)
            self.assertIn(('events/subscribe','rpc_success'),logs)
            self.assertIn(('events/subscribe','unexpected_name_header'),logs)
            self.assertIn(('events/unsubscribe','rpc_success'),logs)
    def test_events_list_mcp_name_header_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            store=EventSubscriptions(Path(d)/'subs.db',
                encryption_key=Fernet.generate_key(),identity_key=b'I'*40)
            app=EventASGI(EventRpc(store),authenticate=lambda h:'cat')
            async def call():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testing') as c:
                    return await c.post('/mcp',
                      json={'jsonrpc':'2.0','id':2,'method':'events/list',
                       'params':{'_meta':{'io.modelcontextprotocol/protocolVersion':'2026-07-28',
                                        'io.modelcontextprotocol/clientCapabilities':{}}}},
                      headers={'content-type':'application/json','mcp-protocol-version':'2026-07-28',
                               'mcp-method':'events/list','mcp-name':'life.location.changed'})
            self.assertEqual(asyncio.run(call()).status_code,400)

if __name__=='__main__': unittest.main()
