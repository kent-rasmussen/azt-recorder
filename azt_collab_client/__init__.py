"""
azt_collab_client — thin client library for azt_collabd.

Ops that go through the server return a ``Result`` (structured status
codes + params); the caller calls ``translate_result(result)`` for
display. ``Result.has(S.PUSHED)`` etc. is the way to drive business
logic — no more substring matching on log strings.
"""

from . import status as S
from .status import Status, Result
from .translate import translate_status, translate_result, set_translator
from .rpc import call, health, ServerUnavailable


def configure(app_id: str):
    """Reserved for step 5: store app identity for logging / provider
    routing. Currently a no-op."""
    return None


def is_online():
    """Ask the server whether it has internet access."""
    try:
        resp = call('GET', '/v1/online')
    except ServerUnavailable:
        return False
    return bool(resp.get('online'))


def sync_repo(project_dir, username, token, contributor):
    """Route a sync job to azt_collabd. Returns a ``Result``.

    On transport failure, returns a Result containing a single synthetic
    Status — callers can still call ``translate_result`` to get a human
    message, or check for ``'SERVER_UNAVAILABLE'`` via ``result.has``.
    """
    try:
        resp = call('POST', '/v1/sync', {
            'project_dir': project_dir,
            'username': username,
            'token': token,
            'contributor': contributor,
        })
    except ServerUnavailable as ex:
        return Result(statuses=[Status(
            'SERVER_UNAVAILABLE', {'error': str(ex)})])
    if resp.get('ok'):
        return Result.from_dict(resp.get('result') or {})
    return Result(statuses=[Status(
        'SERVER_ERROR', {'error': resp.get('error', 'unknown')})])


__all__ = [
    'configure', 'is_online', 'sync_repo',
    'Status', 'Result', 'S',
    'translate_status', 'translate_result', 'set_translator',
    'ServerUnavailable',
]
