"""
Loopback HTTP/JSON server — step 2 scaffolding.

Binds 127.0.0.1 on an OS-assigned port and writes
``$AZT_HOME/server.json`` with ``{port, token, pid, version}`` so clients
can discover the endpoint. Every request (except ``GET /v1/health``)
requires ``Authorization: Bearer <token>``.

Endpoints so far:
    GET  /v1/health            → unauthenticated liveness probe
    GET  /v1/online            → wraps net._has_internet
    POST /v1/sync              → wraps repo.sync_repo
                                 body: {project_dir, username, token, contributor}

Further endpoints arrive with later migration steps. The server holds no
state beyond Handler._token; every call passes the full parameters today.
Project registry / credentials move server-side in steps 6–7.
"""

import http.server
import json
import os
import secrets
import signal
import socketserver
import sys
import threading

from .net import _has_internet
from .paths import azt_home, server_info_path
from .repo import sync_repo as _sync_repo

_VERSION = "0.1.0"


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"azt_collabd/{_VERSION}"
    _token: str = ""   # populated by run()

    def log_message(self, fmt, *args):
        # Silence default stderr access log; still emit via print for errors
        pass

    # ── helpers ─────────────────────────────────────────────────────────

    def _auth_ok(self):
        hdr = self.headers.get('Authorization', '')
        prefix = 'Bearer '
        if not hdr.startswith(prefix):
            return False
        return secrets.compare_digest(hdr[len(prefix):], type(self)._token)

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):
        n = int(self.headers.get('Content-Length', '0') or '0')
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except Exception:
            return None

    # ── dispatch ────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == '/v1/health':
            return self._send_json(200, {
                "ok": True, "version": _VERSION, "pid": os.getpid()})
        if not self._auth_ok():
            return self._send_json(401, {"ok": False, "error": "unauthorized"})
        if self.path == '/v1/online':
            return self._send_json(200, {"ok": True, "online": _has_internet()})
        return self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if not self._auth_ok():
            return self._send_json(401, {"ok": False, "error": "unauthorized"})
        body = self._read_json()
        if body is None:
            return self._send_json(400, {"ok": False, "error": "bad_json"})
        if self.path == '/v1/sync':
            return self._do_sync(body)
        return self._send_json(404, {"ok": False, "error": "not_found"})

    # ── handlers ────────────────────────────────────────────────────────

    def _do_sync(self, body):
        project_dir = body.get('project_dir')
        if not project_dir:
            return self._send_json(400, {"ok": False,
                                         "error": "missing_project_dir"})
        username = body.get('username', '')
        token = body.get('token', '')
        contributor = body.get('contributor', 'Recorder')
        try:
            res = _sync_repo(project_dir, username, token, contributor)
        except Exception as ex:
            return self._send_json(500, {"ok": False, "error": str(ex)})
        return self._send_json(200, {"ok": True, "result": res.to_dict()})


class _ThreadingHTTPServer(socketserver.ThreadingMixIn,
                           http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run(host='127.0.0.1', port=0):
    """Start the server. Blocks until interrupted. Writes server.json on
    bind and removes it on shutdown."""
    home = azt_home()
    os.makedirs(home, exist_ok=True)
    token = secrets.token_urlsafe(32)
    _Handler._token = token
    httpd = _ThreadingHTTPServer((host, port), _Handler)
    bound_port = httpd.server_address[1]
    info = {
        "port": bound_port,
        "token": token,
        "pid": os.getpid(),
        "version": _VERSION,
    }
    info_path = server_info_path()
    fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(info, f)
    print(f'[azt_collabd] listening on {host}:{bound_port} '
          f'(home={home})', flush=True)

    def _graceful(signum, frame):
        # shutdown() blocks until serve_forever exits, so it must run on a
        # different thread than the signal handler (which is the main
        # thread — same as serve_forever).
        print(f'[azt_collabd] signal {signum}, shutting down', flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError):
            # Not on main thread, or platform doesn't support it
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('[azt_collabd] interrupted', flush=True)
    finally:
        try:
            os.remove(info_path)
        except OSError:
            pass
        httpd.server_close()
