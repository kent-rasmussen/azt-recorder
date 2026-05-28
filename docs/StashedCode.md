# Stashed code — dropped but might come back

Code paths removed from `main.py` that the user wants
preserved so they can be reinstated cheaply after evaluating
the UI without them. Each section is a self-contained
drop-in recipe: the snippet, where it goes, and the visible
rendering it produces.

Tracked here (not in git history alone) because git history
mixes the drop with other 1.47.0 contract-conformance changes
and isn't a clean revert target.

---

## Timestamp prefix for the sync-status indicator

**Dropped in 1.47.0** when the v0.47.0 rendering recipe took
over `_sync_status_info`. The user wanted to see the badge
without the timestamp before deciding whether to keep it.

**Why someone might want it back**: in the pre-1.47.0 badge,
the timestamp answered "when did github last accept a push
for this project?" at a glance. Useful for noticing
"haven't pushed in 3 days, do I have signal?" without having
to dig into settings.

**Restoration recipe** — splice into `_sync_status_info`
(main.py ~6009) just before the final `text = label` line.

```python
# Timestamp prefix — formats last_sync into "HH:MM" /
# "yesterday HH:MM" / "N days ago HH:MM". Empty string
# when last_sync == 0 (no successful push yet). Prepended
# to the v0.47.0 label so the user can see the last-push
# time alongside the WAN-N / LAN-N counts.
import datetime
if last_sync:
    dt_sync = datetime.datetime.fromtimestamp(last_sync)
    sync_date = dt_sync.date()
    time_str = dt_sync.strftime('%H:%M')
    days = (datetime.datetime.now().date() - sync_date).days
    if days == 0:
        timestamp = time_str
    elif days == 1:
        timestamp = f'yesterday {time_str}'
    else:
        timestamp = f'{days} days ago {time_str}'
else:
    timestamp = ''
```

Then change:

```python
text = label
```

to:

```python
text = f'{timestamp} {label}' if timestamp else label
```

The badge / suffix concatenation below it doesn't need to
change — `+{n}` and ` · offline` still trail naturally.

**Sample rendered outputs after restoration** (markup
shown plain):

| Rendered | State |
|---|---|
| `OK` | (no timestamp) Never pushed; wan==0 (no remote configured, or fresh project) and lan==0. |
| `14:32 OK` | Pushed at 14:32 today; everything caught up. |
| `yesterday 14:32 WAN-3` | Last push yesterday; 3 commits accumulated since. |
| `3 days ago 14:32 LAN-2 +5 · offline` | Last push 3 days ago; 2 commits unshared on LAN, 5 dirty files, work-offline mode on. |

**Inverse — pre-1.47.0 four `_not_backed_up_text` strings**
that paired with `last_sync == 0` (so a "no timestamp yet"
prefix could still carry meaning). The v0.47.0 model
encodes the same blockers through the WAN-N count, so
these wouldn't need to come back even if the timestamp
prefix does. Catalogued here only because someone reading
old screenshots might want to know what they were:

```python
def _not_backed_up_text(self, status):
    """Pick a 'not backed up' variant that hints at the
    *blocker* so a tap on the indicator lands the user
    where they can actually fix it."""
    try:
        from azt_collab_client import (
            get_contributor, get_credentials_status)
        if not (get_contributor() or '').strip():
            return _tr('add name to back up')
        cred = get_credentials_status() or {}
        host = cred.get('host', 'github')
        connected = bool(cred.get(host, {}).get('connected', False))
        if not connected:
            return _tr('sign in to back up')
        if not (getattr(status, 'remote_url', '') or '').strip():
            return _tr('publish to back up')
    except Exception:
        pass
    return _tr('not backed up')
```

The four msgids (`add name to back up`, `sign in to back
up`, `publish to back up`, `not backed up`) are still in
`locales/fr/LC_MESSAGES/aztrecorder.po` as dead strings;
they cost nothing and would translate correctly if the
function ever returns.
