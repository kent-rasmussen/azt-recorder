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
    """Reserved for later migration steps (app identity for logging /
    provider routing). Currently a no-op."""
    return None


def is_online():
    """Ask the server whether it has internet access."""
    try:
        resp = call('GET', '/v1/online')
    except ServerUnavailable:
        return False
    return bool(resp.get('online'))


# ── Credentials API (server-owned credentials.json) ────────────────────────

def get_credentials_status():
    """Return a dict describing what's configured:
        {host, github: {connected, username, app_installed},
         gitlab: {connected, username}}
    Never contains raw tokens. On transport failure returns an empty
    status so the UI degrades gracefully."""
    try:
        resp = call('GET', '/v1/credentials/status')
    except ServerUnavailable:
        return {'host': 'github',
                'github': {'connected': False, 'username': '',
                           'app_installed': False},
                'gitlab': {'connected': False, 'username': ''}}
    if resp.get('ok'):
        return {k: v for k, v in resp.items() if k != 'ok'}
    return {}


def set_collab_host(host):
    """Persist the user's host selection (github|gitlab)."""
    try:
        call('POST', '/v1/credentials/host', {'host': host})
    except ServerUnavailable:
        pass


def save_github_tokens(token_data, username=''):
    """Persist a device-flow token response + (optional) username."""
    call('POST', '/v1/credentials/github/tokens', {
        'access_token': token_data.get('access_token', ''),
        'refresh_token': token_data.get('refresh_token', ''),
        'username': username,
    })


def mark_github_app_installed(installed=True):
    try:
        call('POST', '/v1/credentials/github/app_installed',
             {'installed': bool(installed)})
    except ServerUnavailable:
        pass


def save_gitlab_credentials(username, token):
    call('POST', '/v1/credentials/gitlab',
         {'username': username, 'token': token})


def migrate_from_prefs(prefs_path):
    """One-shot (idempotent) migration from a legacy prefs.json. The
    server moves gh_*/gl_*/collab_host keys into credentials.json and
    strips them from prefs.json."""
    try:
        resp = call('POST', '/v1/credentials/migrate_from_prefs',
                    {'prefs_path': prefs_path})
    except ServerUnavailable:
        return {'migrated': False, 'reason': 'server_unavailable'}
    return {k: v for k, v in resp.items() if k != 'ok'}


# ── Sync ────────────────────────────────────────────────────────────────────

def sync_repo(project_dir, contributor):
    """Route a sync job to azt_collabd. Returns a ``Result``.

    Server reads the sync credentials from its own store — callers no
    longer pass username/token. If the user hasn't connected to a host,
    the Result contains an AUTH_REQUIRED status."""
    try:
        resp = call('POST', '/v1/sync', {
            'project_dir': project_dir,
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
    'configure', 'is_online',
    'get_credentials_status', 'set_collab_host',
    'save_github_tokens', 'mark_github_app_installed',
    'save_gitlab_credentials', 'migrate_from_prefs',
    'sync_repo',
    'Status', 'Result', 'S',
    'translate_status', 'translate_result', 'set_translator',
    'ServerUnavailable',
]
