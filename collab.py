"""
Git collaboration support for A-Z+T Recorder (part of the A-Z+T suite).

Uses dulwich (pure Python git) so it works on Android without a system git binary.

Workflow
--------
* Publish:  git init in the LIFT project dir, commit everything, push to remote.
* Clone:    clone a remote repo, find the .lift file, open it.
* Pull:     fetch + fast-forward merge from origin.
* Push branch: stage + commit all changes, push to contrib/<name> for a PR.

Authentication
--------------
Uses a GitHub App with device flow for user authentication.
Only the public client_id is embedded in the app — no private key needed.
Tokens are stored locally and refreshed automatically.
"""

import io
import json
import logging
import os
import ssl
import threading
import time

from kivy.clock import Clock

# Suppress dulwich debug/info logging (gitconfig path spam)
logging.getLogger('dulwich').setLevel(logging.WARNING)

# ── GitHub App configuration ─────────────────────────────────────────────────
# Register your GitHub App at https://github.com/settings/apps
# Enable "Device Flow" in the app settings.
# Required permissions: Repository → Administration: Write, Contents: Write
# Set these after creating the app:
from appinfo import APP_SLUG

GITHUB_APP_CLIENT_ID = 'Iv23li66Fo9MBReatv6i'  # set after registering the GitHub App
GITHUB_APP_NAME = APP_SLUG
GITHUB_COLLABORATOR = 'kent-rasmussen'  # auto-added to new repos

# ── SSL fix for Android (missing CA bundle) ──────────────────────────────────
# On Android, p4a doesn't ship system CA certs.  dulwich's
# default_urllib3_manager passes ca_certs=None to urllib3.PoolManager, which
# then tries system certs and fails.  We patch default_urllib3_manager itself
# to inject the certifi CA bundle (or disable verification as a last resort).

def _find_ca_bundle():
    """Return path to a CA bundle, or None."""
    # certifi (preferred — bundled via buildozer requirements)
    try:
        import certifi
        ca = certifi.where()
        if os.path.isfile(ca):
            return ca
    except ImportError:
        pass
    # On Android, certifi's cacert.pem may be inside a zip; extract it
    try:
        import certifi
        import importlib.resources as _res
        # Write the bundle to a writable location
        priv = os.environ.get('ANDROID_PRIVATE', '')
        if priv:
            dest = os.path.join(priv, 'cacert.pem')
            data = _res.read_binary('certifi', 'cacert.pem')
            with open(dest, 'wb') as f:
                f.write(data)
            return dest
    except Exception:
        pass
    # Common Linux / Android system locations
    for path in ('/etc/ssl/certs/ca-certificates.crt',
                 '/system/etc/security/cacerts'):
        if os.path.exists(path):
            return path
    return None


def _patch_dulwich_ssl():
    """Monkey-patch urllib3 and stdlib ssl so all HTTPS works on Android."""
    ca = _find_ca_bundle()

    # Patch urllib3.PoolManager (used by dulwich)
    import urllib3
    _orig_init = urllib3.PoolManager.__init__

    def _patched_init(self, *a, **kw):
        if ca:
            if kw.get('ca_certs') is None:
                kw['ca_certs'] = ca
            kw.setdefault('cert_reqs', 'CERT_REQUIRED')
        else:
            kw['cert_reqs'] = 'CERT_NONE'
            kw.pop('ca_certs', None)
        _orig_init(self, *a, **kw)

    urllib3.PoolManager.__init__ = _patched_init

    # Patch ssl.create_default_context (used by urllib.request.urlopen)
    if ca:
        _orig_ctx = ssl.create_default_context
        def _ctx_with_ca(*a, **kw):
            kw.setdefault('cafile', ca)
            return _orig_ctx(*a, **kw)
        ssl.create_default_context = _ctx_with_ca
        ssl._create_default_https_context = _ctx_with_ca
    else:
        def _unverified_ctx(*a, **kw):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        ssl._create_default_https_context = _unverified_ctx

_dulwich_ssl_patched = False
_dulwich_env_patched = False

def _ensure_gitconfig():
    """Create an empty ~/.gitconfig so dulwich doesn't warn about missing files."""
    global _dulwich_env_patched
    if _dulwich_env_patched:
        return
    _dulwich_env_patched = True
    home = os.environ.get('HOME', '')
    if not home:
        home = os.environ.get('ANDROID_PRIVATE', '')
    if not home:
        return
    os.environ['HOME'] = home
    gitconfig = os.path.join(home, '.gitconfig')
    if not os.path.exists(gitconfig):
        try:
            with open(gitconfig, 'w') as f:
                f.write('[core]\n')
        except OSError:
            pass

def _ensure_ssl():
    """Call once before any dulwich network operation."""
    global _dulwich_ssl_patched
    if not _dulwich_ssl_patched:
        _patch_dulwich_ssl()
        _dulwich_ssl_patched = True
    _ensure_gitconfig()


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
        if 'access_token' in result:
            return result
        error = result.get('error', '')
        if error == 'authorization_pending':
            continue
        elif error == 'slow_down':
            interval = result.get('interval', interval + 5)
            continue
        elif error == 'expired_token':
            raise RuntimeError('Authorization expired. Please try again.')
        elif error == 'access_denied':
            raise RuntimeError('Authorization denied by user.')
        else:
            raise RuntimeError(f'Device flow error: {error}')
    raise RuntimeError('Authorization timed out.')


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


GITHUB_APP_INSTALL_URL = f'https://github.com/apps/{GITHUB_APP_NAME}/installations/new'


def _diagnose_403(token, remote_url):
    """Diagnose a 403 push/pull failure. Returns a human-readable message."""
    if not token:
        return 'Not connected to GitHub. Go to Setup > Connect to GitHub.'
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


# ---------------------------------------------------------------------------
# Token storage helpers (called from main.py app)
# ---------------------------------------------------------------------------

def save_tokens(prefs_path, token_data, username=''):
    """Persist token data to the prefs file."""
    try:
        with open(prefs_path) as f:
            prefs = json.load(f)
    except Exception:
        prefs = {}
    prefs['gh_access_token'] = token_data.get('access_token', '')
    prefs['gh_refresh_token'] = token_data.get('refresh_token', '')
    prefs['gh_token_time'] = time.time()
    if username:
        prefs['gh_username'] = username
    os.makedirs(os.path.dirname(prefs_path), exist_ok=True)
    with open(prefs_path, 'w') as f:
        json.dump(prefs, f)


def get_valid_token(prefs_path):
    """Return (username, access_token) with automatic refresh if expired.
    Returns ('', '') if no token stored or refresh fails."""
    try:
        with open(prefs_path) as f:
            prefs = json.load(f)
    except Exception:
        return '', ''
    token = prefs.get('gh_access_token', '')
    refresh = prefs.get('gh_refresh_token', '')
    username = prefs.get('gh_username', '')
    token_time = prefs.get('gh_token_time', 0)
    if not token:
        return '', ''
    # Access tokens last 8 hours; refresh proactively at 7h
    if time.time() - token_time > 7 * 3600 and refresh:
        try:
            new_data = refresh_access_token(refresh)
            save_tokens(prefs_path, new_data, username)
            token = new_data['access_token']
        except Exception as ex:
            print(f'[collab] token refresh failed: {ex}')
            # Return the old token — it might still work
    return username, token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enc(s):
    return s.encode('utf-8') if isinstance(s, str) else s


def _bytes_path(p):
    return p if isinstance(p, bytes) else os.fsencode(p)


def _find_lift(directory):
    """Return path to the first .lift file found (BFS, skips hidden dirs)."""
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in files:
            if name.endswith('.lift'):
                return os.path.join(root, name)
    return None


def _get_repo(project_dir):
    """Return a dulwich Repo or None."""
    try:
        from dulwich.repo import Repo
        return Repo(project_dir)
    except Exception:
        return None


def _stage_all(repo, project_dir):
    """Stage all modified and untracked files (equivalent to git add -A)."""
    from dulwich import porcelain
    status = porcelain.status(repo)
    paths = []

    for f in status.unstaged:
        paths.append(_bytes_path(f))

    for f in status.untracked:
        rel = f if isinstance(f, str) else f.decode('utf-8', errors='replace')
        full = os.path.join(project_dir, rel)
        if os.path.isfile(full):
            paths.append(_bytes_path(rel))
        elif os.path.isdir(full):
            # dulwich reports untracked dirs as a single entry;
            # walk them to find the actual files
            for root, _dirs, files in os.walk(full):
                for name in files:
                    fp = os.path.join(root, name)
                    rp = os.path.relpath(fp, project_dir)
                    paths.append(_bytes_path(rp))

    if paths:
        porcelain.add(repo, paths=paths)


def _default_author(contributor_name):
    safe = contributor_name.lower().replace(' ', '_')
    return _enc(f'{contributor_name} <{safe}@device>')


def _app_committer():
    """Return committer identity for the A-Z+T Recorder app."""
    return _enc(f'{GITHUB_APP_NAME}[bot] <{GITHUB_APP_NAME}[bot]@users.noreply.github.com>')


def _ensure_remote_repo(remote_url, username, token):
    """Create the remote repo on GitHub/GitLab if it doesn't exist yet.
    On GitHub, also adds GITHUB_COLLABORATOR to the repo.

    Returns (ok, message).
    """
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    # Parse owner/repo from URL like https://github.com/owner/repo.git
    from urllib.parse import urlparse
    parsed = urlparse(remote_url)
    host = parsed.hostname or ''
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    if len(parts) < 2:
        return False, f'Cannot parse owner/repo from {remote_url}'
    owner, repo_name = parts[0], parts[1]

    if 'github.com' in host:
        api_url = 'https://api.github.com/user/repos'
        payload = json.dumps({
            'name': repo_name,
            'private': True,
            'auto_init': False,
        }).encode()
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
        }
    elif 'gitlab' in host:
        api_url = f'https://{host}/api/v4/projects'
        payload = json.dumps({
            'name': repo_name,
            'visibility': 'private',
            'initialize_with_readme': False,
        }).encode()
        headers = {
            'PRIVATE-TOKEN': token,
            'Content-Type': 'application/json',
        }
    else:
        return True, ''   # Unknown host — assume repo exists, let push fail

    created = False
    try:
        req = Request(api_url, data=payload, headers=headers, method='POST')
        urlopen(req, timeout=30)
        created = True
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        # 422 = already exists (GitHub), 400 = already exists (GitLab)
        if e.code in (422, 400) and 'already' in body.lower():
            pass   # Already exists — fine
        else:
            return False, f'Create repo failed ({e.code}): {body[:200]}'
    except (URLError, OSError) as e:
        return False, f'Create repo failed: {e}'

    # Add collaborator on GitHub repos
    if 'github.com' in host and GITHUB_COLLABORATOR:
        try:
            add_collaborator(owner, repo_name, GITHUB_COLLABORATOR, token)
        except Exception as ex:
            print(f'[collab] add collaborator warning: {ex}')

    msg = f'Created remote repository {owner}/{repo_name}.' if created else ''
    return True, msg


# ---------------------------------------------------------------------------
# Public API (called from CollabScreen — may run in a worker thread)
# ---------------------------------------------------------------------------

def repo_status_summary(project_dir):
    """
    Return (branch, remote_url, n_changes) describing the project directory,
    or None if it is not a git repository.
    """
    try:
        from dulwich import porcelain
        repo = _get_repo(project_dir)
        if repo is None:
            return None

        # Branch
        try:
            branch = porcelain.active_branch(repo).decode('utf-8', errors='replace')
        except Exception:
            refs = repo.refs.get_symrefs()
            head_ref = refs.get(b'HEAD', b'')
            branch = head_ref.decode('utf-8').replace('refs/heads/', '') or '(detached)'

        # Remote
        try:
            remote_url = repo.get_config().get(
                (b'remote', b'origin'), b'url'
            ).decode('utf-8')
        except KeyError:
            remote_url = ''

        # Pending changes
        try:
            st = porcelain.status(repo)
            n = (len(st.staged.get('add', [])) +
                 len(st.staged.get('modify', [])) +
                 len(st.staged.get('delete', [])) +
                 len(st.unstaged) +
                 len(st.untracked))
        except Exception:
            n = 0

        return branch, remote_url, n
    except Exception:
        return None


def init_repo(project_dir, remote_url, username, token,
              branch='main', contributor_name='Recorder'):
    """
    Initialize a git repo in project_dir, commit everything, set remote, push.
    Returns a human-readable log string.
    """
    _ensure_ssl()
    from dulwich import porcelain
    from dulwich.repo import Repo
    log = []

    # Init (idempotent)
    repo = _get_repo(project_dir)
    if repo is None:
        repo = porcelain.init(project_dir)
        log.append('Initialized git repository.')
    else:
        log.append('Repository already initialized.')

    # .gitignore
    gitignore = os.path.join(project_dir, '.gitignore')
    if not os.path.exists(gitignore):
        with open(gitignore, 'w') as fh:
            fh.write('__pycache__/\n*.pyc\n.buildozer/\nenv/\n.DS_Store\nimage_cache/\n')
        log.append('Created .gitignore.')

    # Stage + commit
    _stage_all(repo, project_dir)
    author = _default_author(contributor_name)
    committer = _app_committer()
    try:
        sha = porcelain.commit(
            repo,
            message=_enc(f'Initial commit by {contributor_name}'),
            author=author, committer=committer,
        )
        sha_str = sha[:8].decode() if isinstance(sha, bytes) else str(sha)[:8]
        log.append(f'Committed ({sha_str}).')
    except Exception as exc:
        log.append(f'Commit: {exc}')

    # Set or update remote origin
    try:
        existing = repo.get_config().get((b'remote', b'origin'), b'url').decode()
        if existing != remote_url:
            config = repo.get_config()
            config.set((b'remote', b'origin'), b'url', _enc(remote_url))
            config.write_to_path()
            log.append(f'Remote updated to {remote_url}')
        else:
            log.append(f'Remote: {existing}')
    except KeyError:
        porcelain.remote_add(repo, 'origin', remote_url)
        log.append(f'Remote set to {remote_url}')

    # Ensure HEAD points to the desired branch
    desired_ref = _enc(f'refs/heads/{branch}')
    try:
        head_ref = repo.refs.get_symrefs().get(b'HEAD', b'')
        if head_ref != desired_ref:
            # Rename the current branch to the desired name
            head_sha = repo.head()
            repo.refs[desired_ref] = head_sha
            repo.refs.set_symbolic_ref(b'HEAD', desired_ref)
    except Exception:
        pass

    # Ensure remote repo exists (create on GitHub/GitLab if needed)
    ok, msg = _ensure_remote_repo(remote_url, username, token)
    if msg:
        log.append(msg)
    if not ok:
        return '\n'.join(log)

    # Push — let dulwich resolve the active branch rather than a manual refspec
    try:
        porcelain.push(
            repo, remote_url,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        log.append(f'Pushed to {remote_url} (branch: {branch}).')
    except Exception as exc:
        log.append(f'Push failed: {exc}')

    return '\n'.join(log)


class _ProgressStream(io.RawIOBase):
    """Captures git protocol progress lines and forwards to a callback.

    Dulwich writes progress messages like ``Receiving objects:  75% (30/40)\\r``
    to *errstream*.  This stream buffers them and calls *on_progress(line)* on
    each complete line (delimited by ``\\r`` or ``\\n``).
    """

    def __init__(self, on_progress=None):
        self._callback = on_progress
        self._buf = b''

    def write(self, data):
        if not data:
            return 0
        self._buf += data
        while b'\r' in self._buf or b'\n' in self._buf:
            # Split on whichever delimiter comes first
            ri = self._buf.find(b'\r')
            ni = self._buf.find(b'\n')
            if ri == -1:
                idx = ni
            elif ni == -1:
                idx = ri
            else:
                idx = min(ri, ni)
            line = self._buf[:idx].decode('utf-8', errors='replace').strip()
            self._buf = self._buf[idx + 1:]
            if line and self._callback:
                self._callback(line)
        return len(data)

    def writable(self):
        return True


def clone_repo(remote_url, dest_dir, username, token, on_progress=None):
    """
    Clone remote_url into dest_dir.
    Returns (lift_path_or_None, log_string).
    *on_progress* is called with status strings from the git protocol.
    """
    _ensure_ssl()
    from dulwich import porcelain
    log = []

    errstream = _ProgressStream(on_progress) if on_progress else io.BytesIO()

    # Remove any previous dest so clone starts fresh
    if os.path.exists(dest_dir):
        import shutil
        shutil.rmtree(dest_dir)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        porcelain.clone(
            remote_url, dest_dir,
            username=username, password=token,
            errstream=errstream,
        )
        log.append(f'Cloned to {dest_dir}')
    except Exception as exc:
        log.append(f'Clone failed: {exc}')
        return None, '\n'.join(log)

    lift_path = _find_lift(dest_dir)
    if lift_path:
        log.append(f'Found: {os.path.basename(lift_path)}')
    else:
        log.append('No .lift file found in cloned repository.')
    return lift_path, '\n'.join(log)


def pull_repo(project_dir, username, token):
    """
    Pull (fetch + fast-forward) from origin. Returns log string.
    """
    _ensure_ssl()
    from dulwich import porcelain
    log = []
    repo = _get_repo(project_dir)
    if repo is None:
        return 'Not a git repository.'
    try:
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        return 'No remote configured. Publish the project first.'
    try:
        porcelain.pull(
            repo, remote_url,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        log.append('Pulled latest changes from origin.')
    except Exception as exc:
        log.append(f'Pull failed: {exc}')
    return '\n'.join(log)


def commit_and_push_branch(project_dir, username, token, contributor_name):
    """
    Stage all changes, commit, and push to contrib/<contributor_name>.
    The reviewer merges via a pull request on the hosting service.
    Returns log string.
    """
    _ensure_ssl()
    from dulwich import porcelain
    log = []
    repo = _get_repo(project_dir)
    if repo is None:
        return 'Not a git repository. Publish the project first.'
    try:
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        return 'No remote configured. Publish the project first.'

    safe = (contributor_name.lower()
            .replace(' ', '_').replace('/', '_') or 'contributor')
    branch_name = f'contrib/{safe}'
    branch_ref = _enc(f'refs/heads/{branch_name}')

    # Create / switch to contrib branch
    try:
        if branch_ref not in repo.refs:
            repo.refs[branch_ref] = repo.head()
        repo.refs.set_symbolic_ref(b'HEAD', branch_ref)
        log.append(f'On branch {branch_name}.')
    except Exception as exc:
        log.append(f'Branch error: {exc}')

    # Stage all
    _stage_all(repo, project_dir)
    log.append('Staged all changes.')

    # Commit
    author = _default_author(contributor_name)
    committer = _app_committer()
    try:
        porcelain.commit(
            repo,
            message=_enc(f'Audio recordings by {contributor_name}'),
            author=author, committer=committer,
        )
        log.append('Committed.')
    except Exception as exc:
        msg = str(exc).lower()
        if 'nothing' in msg or 'empty' in msg or 'no changes' in msg:
            log.append('Nothing new to commit.')
        else:
            log.append(f'Commit: {exc}')

    # Push
    refspec = _enc(f'refs/heads/{branch_name}:refs/heads/{branch_name}')
    try:
        porcelain.push(
            repo, remote_url, refspec,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        log.append(f'Pushed {branch_name}.')
        log.append('Open your git host to create a pull request.')
    except Exception as exc:
        log.append(f'Push failed: {exc}')

    return '\n'.join(log)


def sync_repo(project_dir, username, token, contributor_name):
    """
    Pull from origin, then stage+commit+push local changes.
    Combines pull and push into a single operation for the Sync button.
    Returns log string.
    """
    _ensure_ssl()
    from dulwich import porcelain
    log = []
    repo = _get_repo(project_dir)
    if repo is None:
        return 'Not a git repository. Publish the project first.'
    try:
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        return 'No remote configured. Publish the project first.'

    # Pull
    try:
        porcelain.pull(
            repo, remote_url,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        log.append('Pulled latest changes.')
    except Exception as exc:
        if '403' in str(exc):
            log.append(f'Pull failed: {_diagnose_403(token, remote_url)}')
            return '\n'.join(log)  # no point continuing
        log.append(f'Pull failed: {exc}')

    # Stage all
    _stage_all(repo, project_dir)

    # Commit only if there are staged changes
    st = porcelain.status(repo)
    has_staged = any(st.staged.get(k) for k in ('add', 'modify', 'delete'))
    if has_staged:
        author = _default_author(contributor_name)
        committer = _app_committer()
        try:
            porcelain.commit(
                repo,
                message=_enc(f'Audio recordings by {contributor_name}'),
                author=author, committer=committer,
            )
            log.append('Committed local changes.')
        except Exception as exc:
            log.append(f'Commit: {exc}')
    else:
        log.append('No local changes to commit.')

    # Push
    try:
        branch = porcelain.active_branch(repo).decode('utf-8', errors='replace')
    except Exception:
        branch = 'main'
    refspec = _enc(f'refs/heads/{branch}:refs/heads/{branch}')
    try:
        porcelain.push(
            repo, remote_url, refspec,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        log.append(f'Pushed to {branch}.')
    except Exception as exc:
        if '403' in str(exc):
            log.append(f'Push failed: {_diagnose_403(token, remote_url)}')
        else:
            log.append(f'Push failed: {exc}')

    return '\n'.join(log)


def _stage_audio(repo, project_dir):
    """Stage only new/modified audio files (audio/ dir and .lift file changes)."""
    from dulwich import porcelain
    status = porcelain.status(repo)
    paths = []

    def _is_audio_or_lift(p):
        s = p if isinstance(p, str) else p.decode('utf-8', errors='replace')
        return (s.startswith('audio/') or s.startswith('images/')
                or s == 'audio' or s == 'images'
                or s.endswith('.lift'))

    for f in status.unstaged:
        if _is_audio_or_lift(f):
            paths.append(_bytes_path(f))

    for f in status.untracked:
        rel = f if isinstance(f, str) else f.decode('utf-8', errors='replace')
        if not _is_audio_or_lift(rel):
            continue
        full = os.path.join(project_dir, rel)
        if os.path.isfile(full):
            paths.append(_bytes_path(rel))
        elif os.path.isdir(full):
            for root, _dirs, files in os.walk(full):
                for name in files:
                    fp = os.path.join(root, name)
                    rp = os.path.relpath(fp, project_dir)
                    paths.append(_bytes_path(rp))

    if paths:
        porcelain.add(repo, paths=paths)
    return len(paths)


def _has_internet():
    """Quick check for internet connectivity."""
    import socket
    for host in ('github.com', 'gitlab.com'):
        try:
            socket.create_connection((host, 443), timeout=3).close()
            return True
        except OSError:
            continue
    return False


def commit_audio_and_sync(project_dir, contributor_name, username, token):
    """Stage + commit audio files, then sync if internet is available.

    Designed to be called in a background thread on page transitions.
    Returns log string (for debugging; not shown to user).
    """
    from dulwich import porcelain
    repo = _get_repo(project_dir)
    if repo is None:
        return 'No repo'

    # Stage audio and .lift changes only
    n = _stage_audio(repo, project_dir)
    if n == 0:
        # Nothing new to commit; still try to sync if online
        if _has_internet():
            try:
                _ensure_ssl()
                remote_url = repo.get_config().get(
                    (b'remote', b'origin'), b'url'
                ).decode('utf-8')
                return sync_repo(project_dir, username, token, contributor_name)
            except Exception:
                pass
        return 'No new audio'

    # Commit
    author = _default_author(contributor_name)
    committer = _app_committer()
    try:
        porcelain.commit(
            repo,
            message=_enc(f'Audio recordings by {contributor_name}'),
            author=author, committer=committer,
        )
    except Exception as exc:
        return f'Commit failed: {exc}'

    # Sync if there's internet
    if not _has_internet():
        return 'Committed locally (offline)'

    try:
        _ensure_ssl()
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        return 'Committed (no remote configured)'

    try:
        branch = porcelain.active_branch(repo).decode('utf-8', errors='replace')
    except Exception:
        branch = 'main'

    # Pull first (fetch + merge) so push won't be rejected
    try:
        porcelain.pull(
            repo, remote_url,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
    except Exception as exc:
        if '403' in str(exc):
            return f'Committed locally, sync failed: {_diagnose_403(token, remote_url)}'
        # Non-fatal — local commit is safe, push may still work
        print(f'[auto-sync] pull warning: {exc}')

    # Push
    refspec = _enc(f'refs/heads/{branch}:refs/heads/{branch}')
    try:
        porcelain.push(
            repo, remote_url, refspec,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        return f'Committed and pushed {n} file(s)'
    except Exception as exc:
        if '403' in str(exc):
            return f'Committed locally, push failed: {_diagnose_403(token, remote_url)}'
        return f'Committed locally, push failed: {exc}'


# ---------------------------------------------------------------------------
# Threading helper used by CollabScreen
# ---------------------------------------------------------------------------

def run_async(func, *args, on_done=None):
    """Run func(*args) in a daemon thread; call on_done(result) on the main thread."""
    def _worker():
        result = func(*args)
        if on_done:
            Clock.schedule_once(lambda dt: on_done(result), 0)
    threading.Thread(target=_worker, daemon=True).start()
