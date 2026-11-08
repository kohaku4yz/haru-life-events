"""One owner-authorized Work event for location OR PulsHealth change.

Explicit type discrimination prevents interpreting a health event as GPS.
No raw location coordinates or full HealthKit history are included.
"""
from __future__ import annotations

from typing import Any

from .location_payload import location_data_valid, LOCATION_PAYLOAD_SCHEMA
from .vitals_event_payload import valid_vitals_data, VITALS_PAYLOAD_SCHEMA

CONTEXT_NAME='life.context.changed'


def valid_context_data(value: object) -> bool:
    if not isinstance(value,dict):return False
    if value.get('kind')=='location':
        return set(value)=={'kind','location'} and location_data_valid(value['location'])
    if value.get('kind')=='vitals':
        return set(value)=={'kind','vitals'} and valid_vitals_data(value['vitals'])
    return False


def wrap_location(data: dict[str,Any]) -> dict[str,Any]:
    wrapped={'kind':'location','location':data}
    if not valid_context_data(wrapped):raise ValueError('invalid location context')
    return wrapped


def wrap_vitals(data: dict[str,Any]) -> dict[str,Any]:
    wrapped={'kind':'vitals','vitals':data}
    if not valid_context_data(wrapped):raise ValueError('invalid vitals context')
    return wrapped


CONTEXT_PAYLOAD_SCHEMA={
    'type':'object',
    'properties':{'kind':{'type':'string','enum':['location','vitals']},
                  'location':LOCATION_PAYLOAD_SCHEMA,
                  'vitals':VITALS_PAYLOAD_SCHEMA},
    'required':['kind'],
    'additionalProperties':False,
    'oneOf':[
        {
            'type':'object',
            'properties':{
                'kind':{'type':'string','const':'location'},
                'location':LOCATION_PAYLOAD_SCHEMA,
            },
            'required':['kind','location'],
            'additionalProperties':False,
        },
        {
            'type':'object',
            'properties':{
                'kind':{'type':'string','const':'vitals'},
                'vitals':VITALS_PAYLOAD_SCHEMA,
            },
            'required':['kind','vitals'],
            'additionalProperties':False,
        },
    ],
}
