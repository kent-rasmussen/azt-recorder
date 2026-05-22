"""
Tombstone — the legacy ``collab`` shim has been retired.

Peer apps (including this one) are now pure clients of the AZT collab
server APK. Use the client library:

    import azt_collab_client
    azt_collab_client.configure(app_id='azt-recorder')

    from azt_collab_client import (
        is_online, check_server_compat, open_server_ui,
        get_credentials_status, set_collab_host,
        github_app_install_url, github_app_client_id,
        github_device_flow_start, github_device_flow_status,
        save_github_tokens, mark_github_app_installed,
        save_gitlab_credentials, migrate_from_prefs,
        list_projects, open_project, register_project,
        derive_langcode, init_project,
        clone_project_start, clone_project_status,
        project_status, sync_project, request_sync, poll_job,
        translate_status, translate_result,
        Status, Result, S, ServerUnavailable, SERVER_APK_INSTALL_URL,
    )

Direct ``azt_collabd.*`` imports are forbidden in peer apps — that
package only ships in the standalone server APK / desktop install.
"""

raise ImportError(
    "the legacy 'collab' module has been retired; "
    "peer apps must import from azt_collab_client instead "
    "(see collab.py docstring for the API surface)")
