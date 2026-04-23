"""
Dulwich operations: init, clone, pull, push, commit, sync, and auto-commit
of audio + LIFT changes. All network ops call net._ensure_ssl() first.
"""

import io
import json
import os

from i18n import _ as _tr

from .net import _ensure_ssl, _has_internet
from .auth import (
    GITHUB_APP_NAME, GITHUB_COLLABORATOR,
    add_collaborator, _diagnose_403,
)


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
    log = []

    # Init (idempotent)
    repo = _get_repo(project_dir)
    if repo is None:
        repo = porcelain.init(project_dir)
        log.append(_tr('Initialized git repository.'))
    else:
        log.append(_tr('Repository already initialized.'))

    # .gitignore
    gitignore = os.path.join(project_dir, '.gitignore')
    if not os.path.exists(gitignore):
        with open(gitignore, 'w') as fh:
            fh.write('__pycache__/\n*.pyc\n.buildozer/\nenv/\n.DS_Store\nimage_cache/\n')
        log.append(_tr('Created .gitignore.'))

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
        log.append(_tr('Committed ({sha}).').format(sha=sha_str))
    except Exception as exc:
        log.append(_tr('Commit: {error}').format(error=exc))

    # Set or update remote origin
    try:
        existing = repo.get_config().get((b'remote', b'origin'), b'url').decode()
        if existing != remote_url:
            config = repo.get_config()
            config.set((b'remote', b'origin'), b'url', _enc(remote_url))
            config.write_to_path()
            log.append(_tr('Remote updated to {url}').format(url=remote_url))
        else:
            log.append(_tr('Remote: {url}').format(url=existing))
    except KeyError:
        porcelain.remote_add(repo, 'origin', remote_url)
        log.append(_tr('Remote set to {url}').format(url=remote_url))

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
        log.append(_tr('Pushed to {url} (branch: {branch}).').format(url=remote_url, branch=branch))
    except Exception as exc:
        log.append(_tr('Push failed: {error}').format(error=exc))

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
                # Split "Phase: detail" onto two lines for narrow displays
                if ':' in line:
                    phase, _, detail = line.partition(':')
                    line = f'{phase}:\n{detail.strip()}'
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
        log.append(_tr('Cloned to {dir}').format(dir=dest_dir))
    except Exception as exc:
        log.append(_tr('Clone failed: {error}').format(error=exc))
        return None, '\n'.join(log)

    lift_path = _find_lift(dest_dir)
    if lift_path:
        log.append(_tr('Found: {file}').format(file=os.path.basename(lift_path)))
    else:
        log.append(_tr('No .lift file found in cloned repository.'))
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
        return _tr('Not a git repository.')
    try:
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        return _tr('No remote configured. Publish the project first.')
    try:
        porcelain.pull(
            repo, remote_url,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        log.append(_tr('Pulled latest changes from origin.'))
    except Exception as exc:
        log.append(_tr('Pull failed: {error}').format(error=exc))
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
        return _tr('Not a git repository. Publish the project first.')
    try:
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        return _tr('No remote configured. Publish the project first.')

    safe = (contributor_name.lower()
            .replace(' ', '_').replace('/', '_') or 'contributor')
    branch_name = f'contrib/{safe}'
    branch_ref = _enc(f'refs/heads/{branch_name}')

    # Create / switch to contrib branch
    try:
        if branch_ref not in repo.refs:
            repo.refs[branch_ref] = repo.head()
        repo.refs.set_symbolic_ref(b'HEAD', branch_ref)
        log.append(_tr('On branch {branch}.').format(branch=branch_name))
    except Exception as exc:
        log.append(_tr('Branch error: {error}').format(error=exc))

    # Stage all
    _stage_all(repo, project_dir)
    log.append(_tr('Staged all changes.'))

    # Commit
    author = _default_author(contributor_name)
    committer = _app_committer()
    try:
        porcelain.commit(
            repo,
            message=_enc(f'Audio recordings by {contributor_name}'),
            author=author, committer=committer,
        )
        log.append(_tr('Committed.'))
    except Exception as exc:
        msg = str(exc).lower()
        if 'nothing' in msg or 'empty' in msg or 'no changes' in msg:
            log.append(_tr('Nothing new to commit.'))
        else:
            log.append(_tr('Commit: {error}').format(error=exc))

    # Push
    refspec = _enc(f'refs/heads/{branch_name}:refs/heads/{branch_name}')
    try:
        porcelain.push(
            repo, remote_url, refspec,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        log.append(_tr('Pushed {branch}.').format(branch=branch_name))
        log.append(_tr('Open your git host to create a pull request.'))
    except Exception as exc:
        log.append(_tr('Push failed: {error}').format(error=exc))

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
        return _tr('Not a git repository. Publish the project first.')
    try:
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        return _tr('No remote configured. Publish the project first.')

    # Pull
    try:
        porcelain.pull(
            repo, remote_url,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        log.append(_tr('Pulled latest changes.'))
    except Exception as exc:
        if '403' in str(exc):
            log.append(_tr('Pull failed: {error}').format(error=_diagnose_403(token, remote_url)))
            return '\n'.join(log)  # no point continuing
        log.append(_tr('Pull failed: {error}').format(error=exc))

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
            log.append(_tr('Committed local changes.'))
        except Exception as exc:
            log.append(_tr('Commit: {error}').format(error=exc))
    else:
        log.append(_tr('No local changes to commit.'))

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
        log.append(_tr('Pushed to {branch}.').format(branch=branch))
    except Exception as exc:
        if '403' in str(exc):
            log.append(_tr('Push failed: {error}').format(error=_diagnose_403(token, remote_url)))
        else:
            log.append(_tr('Push failed: {error}').format(error=exc))

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


def commit_audio_and_sync(project_dir, contributor_name, username, token):
    """Stage + commit audio files, then sync if internet is available.

    Designed to be called in a background thread on page transitions.
    Returns log string (for debugging; not shown to user).
    """
    from dulwich import porcelain
    repo = _get_repo(project_dir)
    if repo is None:
        return _tr('No repo')

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
        return _tr('No new audio')

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
        return _tr('Commit failed: {error}').format(error=exc)

    # Sync if there's internet
    if not _has_internet():
        return _tr('Committed locally (offline)')

    try:
        _ensure_ssl()
        remote_url = repo.get_config().get(
            (b'remote', b'origin'), b'url'
        ).decode('utf-8')
    except KeyError:
        return _tr('Committed (no remote configured)')

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
            return _tr('Committed locally, sync failed: {error}').format(error=_diagnose_403(token, remote_url))
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
        return _tr('Committed and pushed {n} file(s)').format(n=n)
    except Exception as exc:
        if '403' in str(exc):
            return _tr('Committed locally, push failed: {error}').format(error=_diagnose_403(token, remote_url))
        return _tr('Committed locally, push failed: {error}').format(error=exc)
