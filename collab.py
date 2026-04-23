"""
Compatibility shim.

All collaboration logic has moved to the ``azt_collabd`` package. This module
stays as a re-export so existing callers in main.py keep working unchanged.
New code should import from ``azt_collabd`` directly.
"""

from azt_collabd.net import (  # noqa: F401
    _find_ca_bundle, _patch_dulwich_ssl, _ensure_gitconfig, _ensure_ssl,
    _has_internet,
)
from azt_collabd.auth import (  # noqa: F401
    GITHUB_APP_CLIENT_ID, GITHUB_APP_NAME, GITHUB_COLLABORATOR,
    GITHUB_APP_INSTALL_URL,
    device_flow_start, device_flow_poll, refresh_access_token,
    get_github_username, check_app_installed, check_repo_in_installation,
    app_install_url, add_collaborator, _diagnose_403,
)
from azt_collabd.store import (  # noqa: F401
    save_tokens, get_valid_token,
)
from azt_collabd.repo import (  # noqa: F401
    _enc, _bytes_path, _find_lift, _get_repo, _stage_all,
    _default_author, _app_committer, _ensure_remote_repo,
    _ProgressStream, _stage_audio,
    repo_status_summary, init_repo, clone_repo, pull_repo,
    commit_and_push_branch, sync_repo, commit_audio_and_sync,
)
