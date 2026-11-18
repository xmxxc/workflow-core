"""Allowlisted close diagnostics for controlled tests; never ownership authority."""
from pathlib import Path

import bundled_runtime
import host_launch
import wfc_platform


def close_evidence(dispatch, handle):
    evidence = {'dispatch': dispatch, 'generation': handle.get('generation'), 'attempt': handle.get('attempt')}
    snapshot = host_launch.read_receipt(handle.get('snapshot')) or {}
    evidence['snapshot'] = {key: snapshot.get(key) for key in ('state', 'cleanup', 'generation', 'attempt')}
    for role in ('service', 'kernel', 'guardian'):
        identity = handle.get(role) or snapshot.get(role)
        if identity:
            evidence[role] = {'pid': identity.get('pid'), 'creation': identity.get('creation'),
                              'probe': wfc_platform.process_probe(identity['pid'])}
    receipt = host_launch.read_receipt(handle.get('receipt')) or {}
    evidence['host_receipt'] = {key: receipt.get(key) for key in ('category', 'pid', 'processStart', 'exitCode')}
    path = Path(handle['snapshot'])
    traces = {}
    allowed = {'phase', 'elapsed', *bundled_runtime.LifecycleTrace.FIELDS}
    for role in ('runtime', 'guardian'):
        data = host_launch.read_receipt(path.with_suffix('.' + role + '.trace.json')) or {}
        traces[role] = [{key: value for key, value in item.items() if key in allowed}
                        for item in data.get('events', [])[-48:] if isinstance(item, dict)]
    evidence['traces'] = traces
    return evidence


def safe_outcome(value):
    return {key: value[key] for key in ('dispatch', 'operation', 'result', 'round', 'attempt', 'diagnostic')
            if key in value}
