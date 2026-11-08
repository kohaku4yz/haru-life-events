"""Validated PulsHealth localhost handoff -> one Vitals Event candidate.

The PulsHealth relay exposes bounded, source-verified Vitals candidates on a
loopback outbox. This client never reads SQLite and never sends Work webhooks:
the Vitals source worker stages candidates into a gate, and the context worker
delivers them inside the shared context event.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .life_events import Candidate, LifeEventGate
from .settings import health_outbox_url
from .vitals_event_payload import Reading, TRIGGERS, METRIC_KEYS, build_vitals_data

NAME='life.vitals.changed'
EVENT_ID=re.compile(r'^vit_[0-9a-f]{48}$')
MAX_AGE=timedelta(minutes=90)
SOURCE_MAX_AGE=timedelta(minutes=10)
MAX_SOURCE_BYTES=65536


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError('timezone-aware time required')
    return dt.astimezone(timezone.utc)


def _readtime(value: object) -> datetime:
    if not isinstance(value,str):raise ValueError('timestamp must be string')
    try:stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    except (ValueError,OverflowError):raise ValueError('invalid source time') from None
    return _utc(stamp)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise ValueError('health source redirect refused')


def fetch_local_health() -> dict[str,Any]:
    """Loopback only (validated in settings); no redirects or ambient proxy."""
    opener=build_opener(ProxyHandler({}),_NoRedirect())
    req=Request(health_outbox_url(),headers={'Accept':'application/json'},method='GET')
    # A candidate's fresh health snapshot can require multiple verified
    # window reads. Keep the bounded HTTP deadline above that processing
    # time; the upstream Health server still has its own SQLite timeouts.
    with opener.open(req,timeout=15) as response:
        if response.status!=200:raise RuntimeError('health source not ready')
        body=response.read(MAX_SOURCE_BYTES+1)
    if len(body)>MAX_SOURCE_BYTES:
        raise ValueError('oversized Puls source response')
    value=json.loads(body)
    if not isinstance(value,dict):raise ValueError('invalid health source response')
    return value


def validated_candidates(source: object, *, now: datetime) -> list[Candidate]:
    current=_utc(now)
    if not isinstance(source,dict):raise ValueError('source must be an object')
    if source.get('status')=='unavailable':return []
    if (set(source)!={'status','source','generated_at','event_name','candidates','snapshot'}
        or source['status']!='ok' or source['source']!='pulshealth_only'
        or source['event_name']!=NAME):
        raise ValueError('unexpected source identity or fields')
    created=_readtime(source['generated_at'])
    if not -timedelta(minutes=2) <= current-created <= SOURCE_MAX_AGE:
        raise ValueError('stale health source response')
    samples=source['candidates']
    if not isinstance(samples,list) or len(samples)>100:
        raise ValueError('invalid candidate batch')
    snap=source['snapshot']
    if not isinstance(snap,dict) or len(snap)>10:raise ValueError('invalid health snapshot')
    readings:dict[str,Reading]={}
    for key,item in snap.items():
        if not isinstance(key,str) or key not in METRIC_KEYS or not isinstance(item,dict) or set(item)!={'value','observed_at','source'}:
            raise ValueError('invalid metric provenance')
        if item['source']!='pulshealth':raise ValueError('unexpected metric provenance')
        readings[key]=Reading(item['value'],_readtime(item['observed_at']),item['source'])
    out=[]
    known=set()
    for raw in samples:
        if not isinstance(raw,dict) or set(raw)!={'event_id','triggers','observed_at','recorded_at'}:
            raise ValueError('invalid source transition')
        eid=raw['event_id']
        if not isinstance(eid,str) or not EVENT_ID.fullmatch(eid):
            raise ValueError('invalid source event ID')
        if eid in known:continue
        known.add(eid)
        t=_readtime(raw['observed_at'])
        recorded=_readtime(raw['recorded_at'])
        if (t>recorded+timedelta(minutes=2) or recorded>current+timedelta(minutes=2)):
            raise ValueError('future or inconsistent source event')
        # Historical replay is never a delayed Work notification.
        if current-t>MAX_AGE or current-t<timedelta(minutes=-2):
            continue
        triggers=raw['triggers']
        if not (isinstance(triggers,list) and 1<=len(triggers)<=len(TRIGGERS)
                and all(isinstance(name,str) and name in TRIGGERS for name in triggers)
                and len(set(triggers))==len(triggers)):
            raise ValueError('unknown signal type')
        context=build_vitals_data(triggers,t,readings,as_of=current)
        out.append(Candidate(NAME,eid,t,context))
    return out


def stage_from_health(gate: LifeEventGate, *, now: datetime,
                      source: dict[str,Any] | None = None) -> dict[str,int]:
    """Durable, replay-safe local queue stage; never delivers or subscribes."""
    batch=validated_candidates(source if source is not None else fetch_local_health(),now=now)
    accepted=0
    duplicates=0
    for candidate in batch:
        decision=gate.consider(candidate,now=now,enqueue=True)
        if decision.emit:accepted+=1
        else:duplicates+=1
    return {'eligible':len(batch),'staged':accepted,'duplicates':duplicates}
