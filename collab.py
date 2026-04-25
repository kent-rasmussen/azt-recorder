"""
Tombstone — the legacy ``collab`` shim has been retired.

Importers should use the canonical paths:

    from azt_collabd.net    import _has_internet
    from azt_collabd.auth   import (
        device_flow_start, device_flow_poll, get_github_username,
        check_app_installed, app_install_url,
        GITHUB_APP_CLIENT_ID, GITHUB_APP_NAME,
        GITHUB_COLLABORATOR, GITHUB_APP_INSTALL_URL,
    )
    from azt_collabd.repo   import (
        init_repo, clone_repo, pull_repo, sync_repo,
        commit_and_push_branch, commit_audio_and_sync,
        repo_status_summary,
    )
    from azt_collabd.status import Status, Result, AuthError
    from azt_collabd.store  import (
        save_tokens, get_valid_token, get_sync_credentials,
        get_credentials_status,
    )

This file can be deleted once you've grepped to confirm nothing in
your tree still imports from it.
"""

raise ImportError(
    "the legacy 'collab' module has been retired; "
    "import from azt_collabd.* instead "
    "(see collab.py docstring for the mapping)")
