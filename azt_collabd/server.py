"""
Loopback HTTP/JSON server.

Binds 127.0.0.1 on an OS-assigned port and writes
``$AZT_HOME/server.json`` with ``{port, token, pid, version}`` so clients
can discover the endpoint. Every request (except ``GET /v1/health``)
requires ``Authorization: Bearer <token>``.

Endpoints:
    GET  /v1/health                           unauthenticated liveness probe
    GET  /v1/online                           wraps net._has_internet
    GET  /v1/credentials/status               describes what's configured
    POST /v1/credentials/host                 {host}
    POST /v1/credentials/github/tokens        {access_token, refresh_token,
                                               username, token_time?}
    POST /v1/credentials/github/app_installed {installed}
    POST /v1/credentials/gitlab               {username, token}
    POST /v1/credentials/migrate_from_prefs   {prefs_path}
    POST /v1/sync                             {project_dir, contributor}
                                              — creds come from the store
"""

import http.server
import json
import os
import secrets
import signal
import socketserver
import sys
import threading

from . import projects
from . import store
from .net import _has_internet
from .paths import azt_home, server_info_path
from .repo import sync_repo as _sync_repo, repo_status_summary as _repo_status
from .status import Result, Status
from . import status as S

_VERSION = "0.3.0"


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"azt_collabd/{_VERSION}"
    _token: str = ""   # populated by run()

    def log_message(self, fmt, *args):
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
        if self.path == '/v1/credentials/status':
            return self._send_json(200, {"ok": True, **store.get_status()})
        if self.path == '/v1/projects':
            return self._list_projects()
        if self.path.startswith('/v1/projects/'):
            parts = self.path.split('/')
            # /v1/projects/<langcode>               → GET project
            # /v1/projects/<langcode>/status        → GET status
            if len(parts) == 4 and parts[3]:
                return self._get_project(parts[3])
            if len(parts) == 5 and parts[4] == 'status':
                return self._project_status(parts[3])
        return self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if not self._auth_ok():
            return self._send_json(401, {"ok": False, "error": "unauthorized"})
        body = self._read_json()
        if body is None:
            return self._send_json(400, {"ok": False, "error": "bad_json"})
        path = self.path
        if path == '/v1/sync':
            return self._do_sync(body)
        if path == '/v1/credentials/host':
            return self._set_host(body)
        if path == '/v1/credentials/github/tokens':
            return self._set_github_tokens(body)
        if path == '/v1/credentials/github/app_installed':
            return self._set_github_app_installed(body)
        if path == '/v1/credentials/gitlab':
            return self._set_gitlab(body)
        if path == '/v1/credentials/migrate_from_prefs':
            return self._migrate_from_prefs(body)
        if path == '/v1/projects/register':
            return self._register_project(body)
        if path.startswith('/v1/projects/'):
            parts = path.split('/')
            # /v1/projects/<langcode>/sync       → POST sync
            # /v1/projects/<langcode>/last_sync  → POST mark sync time
            if len(parts) == 5 and parts[4] == 'sync':
                return self._project_sync(parts[3], body)
            if len(parts) == 5 and parts[4] == 'last_sync':
                return self._set_project_last_sync(parts[3], body)
        return self._send_json(404, {"ok": False, "error": "not_found"})

    # ── handlers ────────────────────────────────────────────────────────

    def _do_sync(self, body):
        project_dir = body.get('project_dir')
        if not project_dir:
            return self._send_json(400, {"ok": False,
                                         "error": "missing_project_dir"})
        contributor = body.get('contributor', 'Recorder')
        git_user, token = store.get_sync_credentials()
        if not token:
            # Emit a Result so the client can translate uniformly
            res = Result().add(S.AUTH_REQUIRED)
            return self._send_json(200, {"ok": True,
                                         "result": res.to_dict()})
        try:
            res = _sync_repo(project_dir, git_user, token, contributor)
        except Exception as ex:
            return self._send_json(500, {"ok": False, "error": str(ex)})
        return self._send_json(200, {"ok": True, "result": res.to_dict()})

    def _set_host(self, body):
        host = body.get('host', '')
        if host not in ('github', 'gitlab'):
            return self._send_json(400, {"ok": False,
                                         "error": "invalid_host"})
        store.set_collab_host(host)
        return self._send_json(200, {"ok": True})

    def _set_github_tokens(self, body):
        access_token = body.get('access_token', '')
        if not access_token:
            return self._send_json(400, {"ok": False,
                                         "error": "missing_access_token"})
        store.set_github_tokens(
            access_token=access_token,
            refresh_token=body.get('refresh_token', ''),
            username=body.get('username', ''),
            token_time=body.get('token_time'),
        )
        return self._send_json(200, {"ok": True})

    def _set_github_app_installed(self, body):
        store.set_github_app_installed(bool(body.get('installed', False)))
        return self._send_json(200, {"ok": True})

    def _set_gitlab(self, body):
        username = body.get('username', '')
        token = body.get('token', '')
        if not username or not token:
            return self._send_json(400, {"ok": False,
                                         "error": "missing_username_or_token"})
        store.set_gitlab(username, token)
        return self._send_json(200, {"ok": True})

    def _migrate_from_prefs(self, body):
        prefs_path = body.get('prefs_path', '')
        if not prefs_path:
            return self._send_json(400, {"ok": False,
                                         "error": "missing_prefs_path"})
        summary = store.migrate_from_prefs(prefs_path)
        return self._send_json(200, {"ok": True, **summary})

    # ── Project registry ───────────────────────────────────────────────

    def _list_projects(self):
        items = [p.to_dict() for p in projects.list_all()]
        return self._send_json(200, {"ok": True, "projects": items})

    def _get_project(self, langcode):
        p = projects.get(langcode)
        if p is None:
            return self._send_json(404, {"ok": False,
                                         "error": "project_not_found"})
        return self._send_json(200, {"ok": True, "project": p.to_dict()})

    def _register_project(self, body):
        langcode = body.get('langcode', '')
        working_dir = body.get('working_dir', '')
        lift_path = body.get('lift_path', '')
        remote_url = body.get('remote_url', '')
        if not langcode or not working_dir:
            return self._send_json(400, {"ok": False,
                                         "error": "missing_langcode_or_working_dir"})
        # Resolve to absolute paths so the recorder and server agree
        working_dir = os.path.abspath(working_dir)
        if lift_path:
            lift_path = os.path.abspath(lift_path)
        # If remote_url wasn't supplied, try to read it from the working tree
        if not remote_url:
            remote_url = projects.derive_remote_url(working_dir)
        p = projects.register(langcode, working_dir, lift_path, remote_url)
        return self._send_json(200, {"ok": True, "project": p.to_dict()})

    def _project_sync(self, langcode, body):
        p = projects.get(langcode)
        if p is None:
            return self._send_json(404, {"ok": False,
                                         "error": "project_not_found"})
        contributor = body.get('contributor', 'Recorder')
        git_user, token = store.get_sync_credentials()
        if not token:
            res = Result().add(S.AUTH_REQUIRED)
            return self._send_json(200, {"ok": True,
                                         "result": res.to_dict()})
        try:
            res = _sync_repo(p.working_dir, git_user, token, contributor)
        except Exception as ex:
            return self._send_json(500, {"ok": False, "error": str(ex)})
        # Stamp last_sync if we pushed or pulled
        codes = res.codes()
        if ('PUSHED' in codes or 'PULLED' in codes
                or 'COMMITTED_AND_PUSHED' in codes):
            projects.set_last_sync(langcode)
        return self._send_json(200, {"ok": True, "result": res.to_dict()})

    def _project_status(self, langcode):
        p = projects.get(langcode)
        if p is None:
            return self._send_json(404, {"ok": False,
                                         "error": "project_not_found"})
        summary = _repo_status(p.working_dir)
        branch, remote_url, n_changes = ('', '', 0)
        if summary is not None:
            branch, remote_url, n_changes = summary
        return self._send_json(200, {
            "ok": True,
            "langcode": langcode,
            "branch": branch,
            "remote_url": remote_url or p.remote_url,
            "n_changes": n_changes,
            "last_sync": p.last_sync,
            "working_dir": p.working_dir,
            "lift_path": p.lift_path,
        })

    def _set_project_last_sync(self, langcode, body):
        ts = body.get('timestamp')
        projects.set_last_sync(langcode, ts)
        return self._send_json(200, {"ok": True})


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
        print(f'[azt_collabd] signal {signum}, shutting down', flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError):
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
