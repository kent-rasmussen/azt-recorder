# Sync-status indicator — what each label means

This is exhaustive for the text shown between the sync icon
(left) and the gear / settings icon (right) on the recorder
top bar. The label is set by `_sync_status_info` (main.py:5707)
and is the only place this real estate gets touched at runtime.

The full label has up to three parts, concatenated in order:

```
{prefix}( {(suffix)} ){offline_tag}
```

- **prefix** — either a sync timestamp ("when did github last
  accept a push?") or a call-to-action ("not backed up — here's
  the next thing to fix").
- **suffix** — a parenthesised summary of committed history vs
  github and vs LAN peers, optionally with a red dirty-files
  count. Only present when there is committed history to
  summarise; never appears bare-`(OK)` alongside a not-backed-up
  prefix.
- **offline_tag** — empty, ` · offline`, or ` · LAN-only`,
  driven by the daemon-wide work-offline + LAN-share toggles.

Tapping the label fires `app.do_sync()` (a user-initiated sync
per CLIENT_INTEGRATION.md § 17). Empty label → tap still works.

---

## 1. Prefix when github has accepted a push (`last_sync > 0`)

The prefix is a timestamp of the last successful push.

| Rendered | Meaning |
|---|---|
| `14:32` | Pushed today at 14:32. |
| `yesterday 14:32` | Pushed yesterday at 14:32. |
| `3 days ago 14:32` | Pushed N (≥2) calendar days ago. The clock is N midnights, not 24-hour buckets. |

## 2. Prefix when github has never accepted a push (`last_sync == 0`)

A call-to-action string picked by `_not_backed_up_text`
(main.py:5685) to hint at the **next blocker** between the user
and a successful push. Probed in order:

| Rendered | Meaning |
|---|---|
| `add name to back up` | `get_contributor()` is empty. Daemon would refuse commit with `S.CONTRIBUTOR_UNSET`. Tap-to-sync routes to the daemon settings UI's contributor field. |
| `sign in to back up` | Contributor set, but the daemon's GitHub credentials report `connected: false`. Tap-to-sync opens GitHub Connect. |
| `publish to back up` | Signed in, but the project has no `remote_url`. Tap-to-sync opens the daemon's publish flow. |
| `not backed up` | Generic fallback. Reached if the probes above hit an exception or none of the specific blockers apply. |

## 3. Suffix — committed history snapshot

Wrapped in parentheses, appended after the prefix. Driven by
two `ProjectStatus` fields:

- `commits_ahead` — number of local commits not on github yet
  (counting from github's last-fetched `main`).
- `unshared_commits` — of those `commits_ahead`, how many are
  also missing from every paired LAN peer. Zero means every
  local commit lives on at least one other device (github, a
  LAN peer, or both).

This is the part that conveys "is the data safe even if this
phone dies."

| Rendered | Condition | Meaning |
|---|---|---|
| `(OK)` | `commits_ahead == 0` | No local commits exist that github hasn't already seen. Only appears with a timestamp prefix; the "never pushed" path suppresses it. |
| `(+3)` | `commits_ahead == 3`, `unshared_commits == 3` | 3 commits are ahead of github, and none of those 3 are on any LAN peer either. (Says nothing about the rest of project history — much of which is on github.) If this phone dies, those 3 commits are gone. |
| `(LANOK +3)` | `commits_ahead == 3`, `unshared_commits == 0` | 3 commits are ahead of github, and **all 3 of those also exist on at least one paired LAN peer**. If this phone dies, those 3 survive on the LAN peer. |
| `(+1/3)` | `commits_ahead == 3`, `0 < unshared_commits < 3` (here, `unshared == 1`) | 3 commits are ahead of github; of those 3, 1 lives only on this phone and the other 2 also exist on at least one LAN peer. The slash reads "1 unshared, of 3 total ahead." |

The dirty-files addendum:

| Rendered | Condition | Meaning |
|---|---|---|
| `(OK <red>+5</red>)` | `commits_ahead == 0`, `n_changes == 5` | All commits pushed; 5 files have been modified and not yet committed. A passing red means commits-in-progress; a sticky red means the daemon isn't committing (data-loss-risk signal). |
| `(LANOK +1 <red>+2</red>)` | `commits_ahead > 0`, `unshared == 0`, `n_changes > 0` | Same as `(LANOK +1)` but with 2 dirty uncommitted files on top. |
| `(+1 <red>+2</red>)` | Any commits-ahead state + dirty files | The dirty count appends to whatever the commits suffix would have been. |

`n_changes` is the daemon's count of LIFT-touching files in the
working tree that haven't been committed yet. It's displayed in
`#ff4444` red unconditionally when nonzero — short flashes
during normal record/commit cycles are accepted as tuition for
recognising the steady-red "commits aren't happening" state.

## 4. Offline tag

Appended last, after the suffix. Driven by two daemon-wide
toggles on `ProjectStatus`:

- `work_offline` — when true, the daemon's scheduler doesn't
  push, and `sync_project` returns `S.WORK_OFFLINE_ENABLED`.
- `lan_allow_sync` — when true, paired LAN peers still receive
  commits even with `work_offline` on.

The four-cell matrix per CLIENT_INTEGRATION.md § 17b:

| `work_offline` | `lan_allow_sync` | offline_tag | Meaning |
|---|---|---|---|
| off | off | _(empty)_ | Default. Github push runs whenever connectivity allows. |
| off | on | _(empty)_ | Default + LAN delivery; both push paths active. No badge — the user didn't ask for any unusual mode. |
| on | off | ` · offline` | User suspended pushing. Commits accumulate; nothing leaves the device. |
| on | on | ` · LAN-only` | User suspended github push but kept LAN delivery on. Paired phones still receive; github stays untouched. |

## 5. Empty label

`sync_status_label.text` is set to `''` when:

- No project is loaded (`_current_langcode` is empty).
- The server is unreachable for the `project_status` call
  (`project_status` returns `None`). The label silently blanks
  rather than showing a transient-error toast — per § 4 of the
  contract, the bootstrap popup owns "server missing" state.

## 6. Combined examples

Every full label is `{prefix}[ ({suffix})]{offline_tag}`. The
exhaustive cross-product of plausible values:

| Rendered | What it tells the user |
|---|---|
| _(empty)_ | No project loaded, or daemon unreachable. |
| `14:32 (OK)` | Pushed at 14:32 today, no pending work, all clean. |
| `14:32 (OK <red>+5</red>)` | Pushed at 14:32, 5 dirty files awaiting commit. |
| `14:32 (+3)` | Pushed at 14:32, but 3 newer commits exist nowhere but this phone. |
| `14:32 (LANOK +3)` | Pushed at 14:32, 3 newer commits exist on a paired phone but not on github. |
| `14:32 (+1/3)` | Pushed at 14:32, 3 newer commits ahead, 1 of those lives nowhere but this phone (the other 2 are on a LAN peer). |
| `14:32 (+3 <red>+5</red>)` | 3 unpushed commits and 5 dirty files. The dirty count being non-trivial alongside unpushed commits is the "commits aren't draining" signal. |
| `14:32 (OK) · offline` | Pushed at 14:32, work-offline toggled on; no future pushes until the toggle clears. |
| `14:32 (+1) · offline` | Pushed earlier; 1 commit since then is now stranded by the work-offline toggle (and not on any LAN peer). |
| `14:32 (LANOK +1) · LAN-only` | Pushed earlier; 1 commit since then is on a paired phone, github push is suspended by the toggle pair. |
| `14:32 (LANOK +1 <red>+2</red>) · LAN-only` | Same as above plus 2 dirty files. |
| `yesterday 14:32 (LANOK +3)` | Last github push was yesterday; 3 commits since then on LAN. |
| `3 days ago 14:32 (+12)` | Last github push was 3 days ago; 12 unsynced commits stranded only on this phone. Worth investigating. |
| `add name to back up` | Brand-new device or contributor cleared; no commits possible until name is set. |
| `add name to back up <red>+5</red>` | Dirty files exist but cannot be committed until the contributor is set. |
| `sign in to back up` | Contributor set, no GitHub credentials yet. |
| `sign in to back up (LANOK +1)` | Signed-out from github, but 1 commit has already reached a paired LAN peer. |
| `publish to back up` | Signed in but the project has no remote configured. |
| `publish to back up (LANOK +3)` | Project has commits but no github remote; the 3 commits ahead already exist on a paired peer, so those 3 are safe pending publish. |
| `not backed up` | Generic fallback when none of the specific blockers apply. |
| `not backed up (LANOK +3) · LAN-only` | Never github-pushed; user explicitly turned github push off via work-offline; LAN-share still on; 3 commits already delivered to a paired peer. |

## 7. Status codes vs label text

This indicator is **pull-only state**, not a status-code
projection. It reads `ProjectStatus` and renders the snapshot.
Status codes (`S.NOT_A_REPO`, `S.AUTH_REQUIRED`,
`S.WORK_OFFLINE_ENABLED`, etc.) flow through a separate
channel — sync-gesture toasts and route-to-settings dispatch
per CLIENT_INTEGRATION.md § 17 — and never write into
`sync_status_label` directly.

The closest tie is that some "not backed up" prefixes share a
blocker with a status code:

| Label prefix | Related status code (gesture-driven) |
|---|---|
| `add name to back up` | `S.CONTRIBUTOR_UNSET` |
| `sign in to back up` | `S.AUTH_REQUIRED` / `S.AUTH_REFRESH_STALE` |
| `publish to back up` | `S.NOT_A_REPO` / `S.NO_REMOTE` |

But the label doesn't wait to *see* the status code — it polls
`get_contributor()` / `get_credentials_status()` / `remote_url`
on every refresh tick and renders the next-blocker hint
preemptively, so a never-tapped Sync button still tells the
user what's missing.

## 8. Refresh cadence

`_update_sync_status` runs:

- Every 10 s on a `Clock.schedule_interval` while the recorder
  screen is foreground.
- Immediately on `on_resume` (CLIENT_INTEGRATION.md § 14a).
- After every sync gesture's result handler
  (CLIENT_INTEGRATION.md § 17b "Badge refresh obligation").
- After a project load, a project switch, and certain entry
  edits that may have triggered a debounced commit.

The 10 s cadence is the load-bearing one for catching
daemon-driven changes (background push completes,
`work_offline` toggled from another peer, LAN delivery
catches up).
