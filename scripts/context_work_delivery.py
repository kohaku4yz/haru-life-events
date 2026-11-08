"""Unified context route: deliver location AND Vitals as ONE Work event.

Armed by an owner-only marker file plus exactly one verified context
subscription; without both, the context worker keeps the location-only route.
The context worker is the sole Webhook sender; the Vitals source has no WAN access.
"""
from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from haru_life_events.context_payload import CONTEXT_NAME, wrap_location, wrap_vitals
from haru_life_events.event_delivery_v2 import EventDeliveryQueue, LOCATION_MAX_AGE, VITALS_MAX_AGE
from haru_life_events.event_bridge_v2 import EventSubscriptions
from haru_life_events.life_events import LifeEventGate

MARKER='.unified-context.enabled'


def _instant(text:str)->datetime:
    dt=datetime.fromisoformat(text.replace('Z','+00:00'))
    if dt.tzinfo is None or dt.utcoffset() is None:raise ValueError('aware timestamp required')
    return dt.astimezone(timezone.utc)


def marker_armed(path:Path)->bool:
    """True if the owner armed the unified route; ValueError if the marker is malformed."""
    marker=path/MARKER
    if marker.is_symlink():raise ValueError('cutover marker symlink rejected')
    if not marker.exists():return False
    info=marker.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
        or stat.S_IMODE(info.st_mode)!=0o600
        or marker.read_text().strip()!='CONTEXT_ROUTE_V1'):
        raise ValueError('invalid cutover marker')
    return True


def context_enabled(path:Path,store:EventSubscriptions,owner:str,now:datetime)->bool:
    """Only allow transition when owner armed a private file and a verified sub exists."""
    if not marker_armed(path):return False
    if len(store.eligible(owner,CONTEXT_NAME,now))!=1:
        # Fail-safe: old location path is never automatically removed.
        return False
    return True


def _candidate_to_context(event:dict[str,Any])->dict[str,Any]:
    result={
      'eventId':event['eventId'], 'name':CONTEXT_NAME,
      'timestamp':event['timestamp'], 'cursor':None,
    }
    if event['name']=='life.location.changed':
        result['data']=wrap_location(event['data'])
    elif event['name']=='life.vitals.changed':
        result['data']=wrap_vitals(event['data'])
    else:
        raise ValueError('source event not eligible for unified route')
    return result


def transfer_context_pending(*, location:LifeEventGate,
                             vitals:LifeEventGate|None,
                             queue:EventDeliveryQueue,
                             owner:str,now:datetime)->dict[str,int]:
    """Crash-safe source ACK after successful queue enqueue or known replay.

    If subscribed recipient is absent, do not ACK source. Duplicate source IDs
    have stable destination identities. No extra location notifications.
    """
    moved_location=moved_vitals=expired_vitals=expired_location=0
    sources=[(location,'life.location.changed')]
    if vitals is not None:sources.append((vitals,'life.vitals.changed'))
    instant=now.astimezone(timezone.utc)
    for gate,expected in sources:
        for source in gate.pending(limit=100):
            if source.get('name')!=expected:continue
            is_vitals=expected=='life.vitals.changed'
            try:at=_instant(source['timestamp'])
            except (KeyError,TypeError,ValueError):at=None
            if at is None or instant-at>(VITALS_MAX_AGE if is_vitals else LOCATION_MAX_AGE):
                # Unreadable, or too old to be a useful "just changed" notification:
                # drop it so it can neither block the outbox nor burst later.
                if gate.acknowledge_pending(source['eventId']):
                    if is_vitals:expired_vitals+=1
                    else:expired_location+=1
                continue
            if is_vitals and at-instant>timedelta(minutes=2):continue
            unified=_candidate_to_context(source)
            inserted=queue.enqueue(owner=owner,event=unified,now=instant)
            if inserted or queue.already_enqueued(owner=owner,event_id=unified['eventId']):
                gate.acknowledge_pending(source['eventId'])
                if expected=='life.vitals.changed':moved_vitals+=1
                else:moved_location+=1
    return {'location_moved':moved_location,'vitals_moved':moved_vitals,
            'vitals_expired':expired_vitals,'location_expired':expired_location}
