#!/usr/bin/env python3
"""Check a minimal, manually projected Windows evidence ledger, entirely offline.

Exit 0: structurally ready for original-record review; 1: pending; 2: invalid.
No verdict authenticates records or proves WFC caused a window. No collection,
process launch, process control, environment inspection or output-file writing.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re


LIMIT = 512 * 1024
STAGES = {'entry', 'guardian', 'runtime', 'kernel', 'host-supervisor',
          'host-runtime', 'host', 'helper', 'terminal', 'external'}
EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def require(condition):
    if not condition:
        raise ValueError('invalid evidence')


def fields(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected.split()))


def integer(value, maximum):
    require(type(value) is int and 0 <= value <= maximum)
    return value


def stamp(value):
    require(isinstance(value, str))
    match = re.fullmatch(
        r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.(\d{1,7}))?(?:Z|[+-]\d\d:\d\d)', value)
    require(match is not None)
    fraction = match.group(1) or ''
    normalized = value
    if fraction:
        left, right = match.span(1)
        normalized = value[:left] + fraction[:6].ljust(6, '0') + value[right:]
    delta = datetime.fromisoformat(normalized.replace('Z', '+00:00')).astimezone(timezone.utc) - EPOCH
    return ((delta.days * 86400 + delta.seconds) * 10000000 + delta.microseconds * 10
            + (int(fraction[6]) if len(fraction) == 7 else 0))


def identity(value):
    fields(value, 'pid creation')
    pid = integer(value['pid'], 0xffffffff)
    require(pid > 0)
    creation = value['creation']
    require(isinstance(creation, str) and re.fullmatch(r'windows:[1-9]\d{0,19}', creation))
    # FILETIME is a creation identity, not a PID lookup performed after SHOW.
    EPOCH + timedelta(microseconds=int(creation[8:]) // 10)
    return pid, creation


def created(key):
    return int(key[1][8:])


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value


def check(data):
    fields(data, 'version capture_start capture_end clock_uncertainty_ms lost_events entry processes shows')
    require(type(data['version']) is int and data['version'] == 1)
    start, end = stamp(data['capture_start']), stamp(data['capture_end'])
    require(start < end and end - start <= 300 * 10000000)
    uncertainty = integer(data['clock_uncertainty_ms'], 300000) * 10000
    lost = integer(data['lost_events'], 0xffffffff)
    entry = identity(data['entry'])
    require(isinstance(data['processes'], list) and len(data['processes']) <= 128)
    require(isinstance(data['shows'], list) and len(data['shows']) <= 256)
    records = {}
    for process in data['processes']:
        fields(process, 'pid creation parent basename stage creation_flags exited_at')
        key = identity({k: process[k] for k in ('pid', 'creation')})
        require(key not in records)
        require(isinstance(process['basename'], str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', process['basename']))
        require(isinstance(process['stage'], str) and process['stage'] in STAGES)
        parent = None if process['parent'] is None else identity(process['parent'])
        flags = process['creation_flags']
        if flags is not None:
            integer(flags, 0xffffffff)
        exited = None if process['exited_at'] is None else stamp(process['exited_at'])
        require(exited is None or exited >= created(key))
        records[key] = (parent, flags, exited)
    reasons = set()
    if lost:
        reasons.add('capture_events_lost')
    if entry not in records:
        reasons.add('entry_identity_missing')
    if not data['shows']:
        reasons.add('show_event_missing')
    for show in data['shows']:
        fields(show, 'at hwnd class owner')
        at = stamp(show['at'])
        require(isinstance(show['hwnd'], str) and re.fullmatch(r'0x[0-9A-Fa-f]{1,16}', show['hwnd']))
        require(int(show['hwnd'], 16) > 0)
        require(isinstance(show['class'], str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', show['class']))
        owner = identity(show['owner'])
        if at - uncertainty < start or at + uncertainty > end:
            reasons.add('show_outside_capture_or_clock_ambiguous')
        if owner not in records:
            reasons.add('owner_identity_missing')
            continue
        exited = records[owner][2]
        if at - uncertainty < created(owner) or (exited is not None and at + uncertainty >= exited):
            reasons.add('owner_lifetime_or_clock_ambiguous')
        seen = set()
        key = owner
        while key in records:
            if key in seen:
                reasons.add('parent_cycle')
                break
            seen.add(key)
            parent, flags, _ = records[key]
            if flags is None:
                reasons.add('creation_flags_missing')
            if key == entry:
                break
            if parent is None:
                reasons.add('owner_chain_does_not_reach_entry')
                break
            if parent not in records:
                reasons.add('parent_identity_missing')
                break
            parent_exit = records[parent][2]
            if created(parent) > created(key) or (parent_exit is not None and parent_exit <= created(key)):
                reasons.add('parent_lifetime_contradiction')
            key = parent
    return {'verdict': 'pending' if reasons else 'review_ready',
            'native_attribution': 'pending', 'reasons': sorted(reasons)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ledger', type=Path)
    args = parser.parse_args()
    try:
        with args.ledger.open('rb') as source:
            raw = source.read(LIMIT + 1)
        require(len(raw) <= LIMIT)
        data = json.loads(raw, object_pairs_hook=unique_object)
        result = check(data)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        # Do not echo paths, malformed input, key names, or exception text.
        result = {'verdict': 'invalid', 'native_attribution': 'pending',
                  'reasons': ['schema_or_input_invalid']}
    print(json.dumps(result, sort_keys=True))
    return {'review_ready': 0, 'pending': 1, 'invalid': 2}[result['verdict']]


if __name__ == '__main__':
    raise SystemExit(main())
