"""
Token persistence. Currently reads/writes the recorder app's prefs.json; will
move to its own credentials.json in step 6 of the migration plan.
"""

import json
import os
import time

from .auth import refresh_access_token


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
