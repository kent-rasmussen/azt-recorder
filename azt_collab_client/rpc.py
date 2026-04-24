"""
Loopback HTTP transport for the azt_collab_client library.

Reads ``$AZT_HOME/server.json`` (written by azt_collabd at bind time) to
discover ``{port, token}`` and issues authenticated JSON requests.

Auto-spawns the server on demand: if ``server.json`` is missing, or the
server it points to is dead (PID gone or port refused), the client
launches ``python -m azt_collabd`` as a detached subprocess and retries
the call once. Disable by setting ``AZT_CLIENT_AUTOSPAWN=0``.

A future Android ContentProvider transport will probe sibling suite
apps first and fall back to this loopback path.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from .paths import server_info_path


class ServerUnavailable(RuntimeError):
    """Raised when the server cannot be reached even after an autospawn
    attempt."""


_DEFAULT_TIMEOUT = 300  # sync operations can be slow on large repos
_SPAWN_LOCK = threading.Lock()
_HEALTH_TIMEOUT = 1.5     # short probe before handing the endpoint to a call
_SPAWN_WAIT = 5.0         # how long to wait for the server to advertise


def _read_server_info():
    path = server_info_path()
    try:
        with open(path) as f:
            info = json.load(f)
    except FileNotFoundError:
        raise ServerUnavailable(
            f'{path} not found. Start the service: python -m azt_collabd')
    except Exception as ex:
        raise ServerUnavailable(f'cannot read {path}: {ex}')
    if not info.get('port') or not info.get('token'):
        raise ServerUnavailable(f'{path} missing port/token')
    return info


def _pid_alive(pid):
    if not pid:
        return True   # older server.json without pid — trust it
    if not isinstance(pid, int):
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to someone else — still alive
        return True
    except OSError:
        return True


def _server_alive(info):
    """Cheap liveness check: PID exists and /v1/health responds within
    _HEALTH_TIMEOUT seconds."""
    if not _pid_alive(info.get('pid')):
        return False
    url = f'http://127.0.0.1:{info["port"]}/v1/health'
    try:
        with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _autospawn_enabled():
    return os.environ.get('AZT_CLIENT_AUTOSPAWN', '1') != '0'


def _spawn_server():
    """Launch ``python -m azt_collabd`` detached. Returns True if the
    new server advertises itself within ``_SPAWN_WAIT`` seconds."""
    if not _autospawn_enabled():
        return False
    with _SPAWN_LOCK:
        # Maybe another thread/process spawned while we waited
        try:
            if _server_alive(_read_server_info()):
                return True
        except ServerUnavailable:
            pass
        # Remove any stale info file so our probe below only succeeds
        # once the new server has written a fresh one
        try:
            os.remove(server_info_path())
        except OSError:
            pass
        try:
            kwargs = {
                'stdout': subprocess.DEVNULL,
                'stderr': subprocess.DEVNULL,
                'stdin': subprocess.DEVNULL,
                'close_fds': True,
            }
            if hasattr(os, 'setsid'):
                kwargs['start_new_session'] = True
            subprocess.Popen(
                [sys.executable, '-m', 'azt_collabd'], **kwargs)
        except OSError as ex:
            print(f'[azt_collab_client] spawn failed: {ex}')
            return False
        deadline = time.time() + _SPAWN_WAIT
        while time.time() < deadline:
            try:
                info = _read_server_info()
                if _server_alive(info):
                    return True
            except ServerUnavailable:
                pass
            time.sleep(0.1)
        return False


def _call_once(info, method, path, body, timeout):
    url = f'http://127.0.0.1:{info["port"]}{path}'
    headers = {'Authorization': f'Bearer {info["token"]}'}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return json.loads(raw)
        except Exception:
            raise ServerUnavailable(f'HTTP {e.code}: {raw[:200]!r}')
    return json.loads(raw)


def call(method, path, body=None, timeout=_DEFAULT_TIMEOUT):
    """Invoke a server endpoint. Auto-spawns on first failure. Returns
    the parsed JSON response. Raises ``ServerUnavailable`` on
    transport-level failure even after respawn."""
    last_err = None
    for attempt in range(2):
        try:
            info = _read_server_info()
        except ServerUnavailable as ex:
            last_err = ex
            if attempt == 0 and _spawn_server():
                continue
            raise
        try:
            return _call_once(info, method, path, body, timeout)
        except (urllib.error.URLError, OSError) as ex:
            # Connection refused / reset / timeout → the server may
            # have died. Try respawning once.
            last_err = ex
            if attempt == 0 and _spawn_server():
                continue
            raise ServerUnavailable(f'connection failed: {ex}')
    raise ServerUnavailable(str(last_err))


def health():
    """Unauthenticated liveness probe. Returns dict or raises."""
    info = _read_server_info()
    url = f'http://127.0.0.1:{info["port"]}/v1/health'
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError) as e:
        raise ServerUnavailable(f'health check failed: {e}')
