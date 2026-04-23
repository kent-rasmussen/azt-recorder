"""
azt_collab_client — thin client library for azt_collabd.

Step 2 scope: exposes a single real call (`sync_repo`) that routes through
the loopback HTTP transport. Signature matches the old
``azt_collabd.repo.sync_repo`` so callers in main.py can swap the import
without changing call sites. Broader API (open_project, request_sync,
project_status, device-flow proxying, etc.) arrives in later migration
steps.
"""

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
    """Route a sync job to azt_collabd. Returns a human-readable log
    string (same contract as azt_collabd.repo.sync_repo).

    On transport failure, returns an error message prefixed with
    ``'Sync service unavailable:'`` — callers already display a log
    label, so existing error-handling paths in main.py surface it.
    """
    try:
        resp = call('POST', '/v1/sync', {
            'project_dir': project_dir,
            'username': username,
            'token': token,
            'contributor': contributor,
        })
    except ServerUnavailable as ex:
        return f'Sync service unavailable: {ex}'
    if resp.get('ok'):
        return resp.get('log', '')
    return f'Sync service error: {resp.get("error", "unknown")}'


__all__ = [
    'configure', 'is_online', 'sync_repo',
    'ServerUnavailable',
]
