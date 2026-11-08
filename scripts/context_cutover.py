"""Owner-requested one-task context event migration (safe flags).

Reuses the owner's preexisting Work callback only after a real challenge is
verified; never creates an Automation task or prints webhook secrets/URLs.
Does not turn off old location delivery. Optional marker enables routing
only if the new verified context subscription stays eligible.
"""
from __future__ import annotations

import argparse
import base64
import os
import sqlite3
import stat
from datetime import datetime,timezone
from pathlib import Path

from haru_life_events.context_payload import CONTEXT_NAME
from haru_life_events.event_bridge_v2 import EventSubscriptions,PinnedWebhookClient
from haru_life_events.tunnel_events_local import _secret_file
from haru_life_events.settings import events_owner
from scripts.context_work_delivery import MARKER,context_enabled

BASE=Path.home()/'.local/share/haru-life-events'
CONFIG=Path.home()/'.config/haru-life-events'


def _store(*,verified=False):
    if not BASE.is_dir() or not CONFIG.is_dir():raise RuntimeError('event storage unavailable')
    return EventSubscriptions(BASE/'subscriptions.sqlite',
        encryption_key=_secret_file(str(CONFIG/'vault.key')),
        identity_key=_secret_file(str(CONFIG/'identity.key')),
        verifier=PinnedWebhookClient() if verified else None)


def status() -> bool:
    now=datetime.now(timezone.utc)
    store=_store()
    legacy=store.eligible(events_owner(),'life.location.changed',now)
    unified=store.eligible(events_owner(),CONTEXT_NAME,now)
    print('LEGACY_LOCATION_SUBSCRIBERS='+str(len(legacy)))
    print('UNIFIED_CONTEXT_SUBSCRIBERS='+str(len(unified)))
    active=context_enabled(BASE,store,events_owner(),now)
    print('UNIFIED_ROUTING_ACTIVE='+str(active).lower())
    print('WORK_TASKS_CREATED_BY_THIS_SCRIPT=0')
    print('SECRETS_OR_CALLBACK_URLS_EXPOSED=no')
    return active


def subscribe_existing() -> None:
    """Only same owner, same signed callback as existing working Work task."""
    now=datetime.now(timezone.utc)
    store=_store(verified=True)
    old=store.eligible(events_owner(),'life.location.changed',now)
    if len(old)!=1:raise RuntimeError('expected exactly one existing Work location callback')
    current=store.eligible(events_owner(),CONTEXT_NAME,now)
    if current:
        if len(current)!=1 or current[0].callback!=old[0].callback:
            raise RuntimeError('existing unified callback conflicts with old task')
        print('UNIFIED_SUBSCRIBER=already_verified')
        return
    # Real Work callback challenge; fail closed if Work does not recognize
    # a new subscription for the existing task. No trial event is sent.
    delivery={'mode':'webhook','url':old[0].callback,
              'secret':'whsec_'+base64.b64encode(old[0].secret).decode()}
    store.subscribe(events_owner(),{'name':CONTEXT_NAME,'arguments':{},'delivery':delivery},now)
    print('UNIFIED_SUBSCRIBER=verified')
    print('CALLBACK_REUSED=existing_work_task')
    print('WORK_TASKS_CREATED=0')
    print('OLD_SUBSCRIBER_LEFT_ACTIVE=yes')


def arm() -> None:
    # A verified HTTPS callback alone does not prove ChatGPT Work's event
    # task is actually bound to the NEW subscription ID. Require the operator
    # to independently verify a recorded context-canary Task run first.
    if os.getenv('HARU_CONTEXT_WORK_CANARY_RUN_VERIFIED')!='1':
        raise RuntimeError('Work task run for unified canary is not verified; legacy location preserved')
    now=datetime.now(timezone.utc)
    store=_store()
    old=store.eligible(events_owner(),'life.location.changed',now)
    new=store.eligible(events_owner(),CONTEXT_NAME,now)
    if len(new)!=1 or len(old)>1:
        raise RuntimeError('expected precisely one verified Work context callback')
    if old and old[0].callback!=new[0].callback:
        raise RuntimeError('legacy task still uses a conflicting callback')
    # Work can retire the legacy trigger and issue a NEW callback URL as
    # part of an in-place event trigger change. That is expected only after
    # an ACTUAL Work task run from a signed, synthetic context canary.
    # The HARU_CONTEXT_WORK_CANARY_RUN_VERIFIED flag is not derived from an
    # HTTP 2xx or subscription challenge; operator independently checks
    # the Work task's last run time in ChatGPT.
    db=BASE/'deliveries.sqlite'
    with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as connection:
        waiting=connection.execute("SELECT COUNT(*) FROM event_deliveries WHERE state IN ('pending','inflight')").fetchone()[0]
        if waiting:raise RuntimeError('legacy delivery queue has inflight/pending items')
    marker=BASE/MARKER
    if marker.is_symlink():raise RuntimeError('cutover marker symlink')
    if marker.exists():
        if context_enabled(BASE,store,events_owner(),now):
            print('UNIFIED_ROUTING=already_active');return
        raise RuntimeError('existing marker is invalid')
    fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(fd,'w') as file:
            file.write('CONTEXT_ROUTE_V1\n')
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        marker.unlink(missing_ok=True)
        raise
    print('UNIFIED_ROUTING=armed_for_next_location_worker_loop')
    print('LEGACY_LOCATION_SUBSCRIPTION=left_unchanged')


def disarm() -> None:
    marker=BASE/MARKER
    if marker.is_symlink():raise RuntimeError('cutover marker symlink')
    if marker.is_file():
        st=marker.stat()
        if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600:
            raise RuntimeError('not an owner-owned cutover marker')
        marker.unlink()
    print('UNIFIED_ROUTING=disabled')
    print('LEGACY_LOCATION_SUBSCRIPTION=preserved')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['status','subscribe-existing','arm','disarm'])
    args=p.parse_args()
    result={'status':status,'subscribe-existing':subscribe_existing,'arm':arm,'disarm':disarm}[args.command]()
    if args.command=='status' and result is False:
        raise SystemExit(3)  # routing off: Vitals are not being delivered (for monitoring)

if __name__=='__main__':main()
