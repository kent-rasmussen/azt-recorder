"""
Loopback HTTP transport for the azt_collab_client library.

Reads ``$AZT_HOME/server.json`` (written by azt_collabd at bind time) to
discover ``{port, token}`` and issues authenticated JSON requests.

Step 2 scope: the client does not auto-spawn the server. Users start it
with ``python -m azt_collabd``. Auto-spawn + ContentProvider discovery
arrive in step 9.
"""

import json
import os
import urllib.error
import urllib.request

from .paths import server_info_path


class ServerUnavailable(RuntimeError):
    """Raised when the server cannot be reached. Step 2: caller handles
    it and displays a user-visible message. Step 9: client auto-spawns
    the server and only raises after spawn fails."""


_DEFAULT_TIMEOUT = 300  # sync operations can be slow on large repos


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


def call(method, path, body=None, timeout=_DEFAULT_TIMEOUT):
    """Invoke a server endpoint. Returns the parsed JSON response.
    Raises ServerUnavailable on transport-level failure."""
    info = _read_server_info()
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
    except (urllib.error.URLError, OSError) as e:
        raise ServerUnavailable(f'connection failed: {e}')
    return json.loads(raw)


def health():
    """Unauthenticated liveness probe. Returns dict or raises."""
    info = _read_server_info()
    url = f'http://127.0.0.1:{info["port"]}/v1/health'
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError) as e:
        raise ServerUnavailable(f'health check failed: {e}')
