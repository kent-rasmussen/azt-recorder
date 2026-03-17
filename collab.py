"""
Git collaboration support for LIFT Recorder.

Uses dulwich (pure Python git) so it works on Android without a system git binary.

Workflow
--------
* Publish:  git init in the LIFT project dir, commit everything, push to remote.
* Clone:    clone a remote repo, find the .lift file, open it.
* Pull:     fetch + fast-forward merge from origin.
* Push branch: stage + commit all changes, push to contrib/<name> for a PR.
"""

import io
import os
import threading

from kivy.clock import Clock


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

    if paths:
        porcelain.add(repo, paths=paths)


def _default_author(contributor_name):
    safe = contributor_name.lower().replace(' ', '_')
    return _enc(f'{contributor_name} <{safe}@device>')


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
            fh.write('__pycache__/\n*.pyc\n.buildozer/\nenv/\n.DS_Store\n')
        log.append('Created .gitignore.')

    # Stage + commit
    _stage_all(repo, project_dir)
    author = _default_author(contributor_name)
    try:
        sha = porcelain.commit(
            repo,
            message=_enc(f'Initial commit by {contributor_name}'),
            author=author, committer=author,
        )
        sha_str = sha[:8].decode() if isinstance(sha, bytes) else str(sha)[:8]
        log.append(f'Committed ({sha_str}).')
    except Exception as exc:
        log.append(f'Commit: {exc}')

    # Set remote origin
    try:
        existing = repo.get_config().get((b'remote', b'origin'), b'url').decode()
        log.append(f'Remote already set: {existing}')
    except KeyError:
        porcelain.remote_add(repo, 'origin', remote_url)
        log.append(f'Remote set to {remote_url}')

    # Push
    refspec = _enc(f'refs/heads/{branch}:refs/heads/{branch}')
    try:
        porcelain.push(
            repo, remote_url, refspec,
            username=username, password=token,
            errstream=io.BytesIO(),
        )
        log.append(f'Pushed to {remote_url} (branch: {branch}).')
    except Exception as exc:
        log.append(f'Push failed: {exc}')

    return '\n'.join(log)


def clone_repo(remote_url, dest_dir, username, token):
    """
    Clone remote_url into dest_dir.
    Returns (lift_path_or_None, log_string).
    """
    from dulwich import porcelain
    log = []
    try:
        os.makedirs(dest_dir, exist_ok=True)
        porcelain.clone(
            remote_url, dest_dir,
            username=username, password=token,
            errstream=io.BytesIO(),
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
    try:
        porcelain.commit(
            repo,
            message=_enc(f'Audio recordings by {contributor_name}'),
            author=author, committer=author,
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
        log.append(f'Pull failed: {exc}')

    # Stage all
    _stage_all(repo, project_dir)

    # Commit
    author = _default_author(contributor_name)
    try:
        porcelain.commit(
            repo,
            message=_enc(f'Audio recordings by {contributor_name}'),
            author=author, committer=author,
        )
        log.append('Committed local changes.')
    except Exception as exc:
        msg = str(exc).lower()
        if 'nothing' in msg or 'empty' in msg or 'no changes' in msg:
            log.append('No local changes to commit.')
        else:
            log.append(f'Commit: {exc}')

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
        log.append(f'Push failed: {exc}')

    return '\n'.join(log)


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
