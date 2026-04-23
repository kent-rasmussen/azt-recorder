"""
GitHub App device flow, token refresh, app install / repo access checks,
GitLab collaborator add. Uses a GitHub App with device flow — only the
public client_id is embedded in the app.
"""

import json
import time

from appinfo import APP_SLUG
from i18n import _ as _tr

from .net import _ensure_ssl

# ── GitHub App configuration ─────────────────────────────────────────────────
# Register your GitHub App at https://github.com/settings/apps
# Enable "Device Flow" in the app settings.
# Required permissions: Repository → Administration: Write, Contents: Write
# Set these after creating the app:
GITHUB_APP_CLIENT_ID = 'Iv23li66Fo9MBReatv6i'  # set after registering the GitHub App
GITHUB_APP_NAME = APP_SLUG
GITHUB_COLLABORATOR = 'kent-rasmussen'  # auto-added to new repos

GITHUB_APP_INSTALL_URL = f'https://github.com/apps/{GITHUB_APP_NAME}/installations/new'


# ---------------------------------------------------------------------------
# GitHub App Device Flow authentication
# ---------------------------------------------------------------------------

def device_flow_start():
    """Begin device flow. Returns dict with 'user_code', 'verification_uri',
    'device_code', 'interval', 'expires_in' — or raises on error."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    req = Request(
        'https://github.com/login/device/code',
        data=f'client_id={GITHUB_APP_CLIENT_ID}&scope=repo'.encode(),
        headers={'Accept': 'application/json'},
        method='POST',
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def device_flow_poll(device_code, interval=5, expires_in=900):
    """Poll until user authorizes or timeout. Returns token dict or raises.

    Token dict keys: access_token, refresh_token, token_type, etc.
    Blocks the calling thread (run in background).
    """
    _ensure_ssl()
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        data = (
            f'client_id={GITHUB_APP_CLIENT_ID}'
            f'&device_code={device_code}'
            f'&grant_type=urn:ietf:params:oauth:grant-type:device_code'
        ).encode()
        req = Request(
            'https://github.com/login/oauth/access_token',
            data=data,
            headers={'Accept': 'application/json'},
            method='POST',
        )
        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except HTTPError:
            continue
        except OSError:
            # Network glitch (e.g. ECONNREFUSED) — retry
            continue
        if 'access_token' in result:
            return result
        error = result.get('error', '')
        if error == 'authorization_pending':
            continue
        elif error == 'slow_down':
            interval = result.get('interval', interval + 5)
            continue
        elif error == 'expired_token':
            raise RuntimeError(_tr('Authorization expired. Please try again.'))
        elif error == 'access_denied':
            raise RuntimeError(_tr('Authorization denied by user.'))
        else:
            raise RuntimeError(f'Device flow error: {error}')
    raise RuntimeError(_tr('Authorization timed out.'))


def refresh_access_token(refresh_token):
    """Refresh an expired access token. Returns new token dict."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    data = (
        f'client_id={GITHUB_APP_CLIENT_ID}'
        f'&grant_type=refresh_token'
        f'&refresh_token={refresh_token}'
    ).encode()
    req = Request(
        'https://github.com/login/oauth/access_token',
        data=data,
        headers={'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except Exception as ex:
        raise RuntimeError(f'Token refresh network error: {ex}')
    if 'access_token' in result:
        return result
    raise RuntimeError(f'Token refresh failed: {result.get("error", "unknown")}')


def get_github_username(token):
    """Return the authenticated user's GitHub username."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    req = Request(
        'https://api.github.com/user',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get('login', '')
    except Exception as ex:
        print(f'[collab] get_github_username failed: {ex}')
        return ''


def check_app_installed(token):
    """Check if the GitHub App is installed for the authenticated user.
    Returns dict: {'installed': bool, 'installation_id': int|None,
                    'all_repos': bool}."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    result = {'installed': False, 'installation_id': None, 'all_repos': False}
    req = Request(
        'https://api.github.com/user/installations',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        for inst in data.get('installations', []):
            if inst.get('app_slug') == GITHUB_APP_NAME:
                result['installed'] = True
                result['installation_id'] = inst.get('id')
                # 'all' means all repos, 'selected' means specific repos
                result['all_repos'] = (
                    inst.get('repository_selection') == 'all')
                break
    except HTTPError:
        pass
    return result


def check_repo_in_installation(token, installation_id, owner, repo_name):
    """Check if a specific repo is accessible to the app installation.
    Returns True if accessible, False otherwise."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    # List repos accessible to the installation (paginated, check first page)
    req = Request(
        f'https://api.github.com/user/installations/{installation_id}'
        f'/repositories?per_page=100',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        for r in data.get('repositories', []):
            if r.get('full_name', '').lower() == f'{owner}/{repo_name}'.lower():
                return True
        return False
    except HTTPError:
        return False


def app_install_url(installation_id=None):
    """Return the URL to install or configure the GitHub App."""
    if installation_id:
        return f'https://github.com/settings/installations/{installation_id}'
    return f'https://github.com/apps/{GITHUB_APP_NAME}/installations/new'


def _diagnose_403(token, remote_url):
    """Diagnose a 403 push/pull failure. Returns a human-readable message."""
    if not token:
        return _tr('Not connected to GitHub. Go to Setup > Connect to GitHub.')
    info = check_app_installed(token)
    if not info['installed']:
        return (f'App not installed. Visit {GITHUB_APP_INSTALL_URL} '
                f'and select "All repositories".')
    # App is installed — check if this repo is accessible
    install_id = info['installation_id']
    if not info['all_repos']:
        # Extract owner/repo from remote URL
        import re
        m = re.search(r'github\.com[/:]([^/]+)/([^/.]+)', remote_url)
        if m:
            owner, repo_name = m.group(1), m.group(2)
            if not check_repo_in_installation(token, install_id, owner, repo_name):
                settings_url = app_install_url(install_id)
                return (f'App not authorized for {owner}/{repo_name}. '
                        f'Add it at {settings_url}')
    return f'Access denied (403). Check app permissions at {app_install_url(install_id)}'


def add_collaborator(owner, repo_name, collaborator, token):
    """Add *collaborator* to *owner/repo_name* on GitHub. Silently succeeds if
    already a collaborator or if the invitation was already sent."""
    _ensure_ssl()
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    url = f'https://api.github.com/repos/{owner}/{repo_name}/collaborators/{collaborator}'
    req = Request(
        url,
        data=json.dumps({'permission': 'push'}).encode(),
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
        },
        method='PUT',
    )
    try:
        with urlopen(req, timeout=15) as resp:
            pass  # 201 = invited, 204 = already collaborator
    except HTTPError as e:
        if e.code not in (204, 422):  # 422 = already invited
            print(f'[collab] add collaborator failed ({e.code}): {e.read()[:200]}')
