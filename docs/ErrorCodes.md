# Error correlation — one row per situation

Cross-reference of error situations in `main.py` + the symlinked
`azt_collab_client/` package, correlating the situation with:

- the daemon status code (if any) that surfaces it,
- the user-facing string (verbatim msgid),
- the log prefix written to logcat / stderr.

Grouped by subdomain; each row is one situation. "—" means the
column doesn't apply (no daemon code, no user surface, or no log
prefix respectively).

See also: `docs/StatusCodes.md` (sync-status indicator rendering)
and `azt_collab_client/CLIENT_INTEGRATION.md` § 17 (sync-result
routing table — the authoritative source for `S.X` semantics).

## Recording lifecycle

| Situation | Code | User-facing | Log |
|---|---|---|---|
| Quality floor hit (lowest profile failed) | — | "Recording failed at the lowest quality. Please try again." | `[record]` |
| Quality degraded one step | — | "Recording failed; quality lowered. Please try again." | `[record]` |
| MediaRecorder prepare/start >10 s | — | "Recording setup timed out. Please try again." | `[record]` |
| Push-to-talk press <200 ms | — | "Hold the button to record" | — |
| Android silent-input detected | — | "No audio detected — another app may have the microphone. Close any recording / call apps and try again." | `[record]` |
| `set_audio` LIFT write raised | — | "Audio captured but reference not saved — will retry automatically." | `[record]` |

## Sync routing (`commit_project` / `sync_project` result codes)

| Situation | Code | User-facing (user-Sync) | Log (auto-sync) |
|---|---|---|---|
| No git repo | `S.NOT_A_REPO` | "Not a git repository. Publish the project first." | `[auto-sync]` (silent) |
| No remote | `S.NO_REMOTE` | "No remote configured. Publish the project first." | `[auto-sync]` (silent) |
| GitHub not connected | `S.AUTH_REQUIRED` | "Not connected to GitHub. Go to Setup > Connect to GitHub." | `[auto-sync]` (silent) |
| Contributor name unset | `S.CONTRIBUTOR_UNSET` | "Please set your name in the sync settings before publishing or syncing." | `[auto-sync]` (silent) |
| Work-offline toggle on | `S.WORK_OFFLINE_ENABLED` | "Work-offline mode is on. Turn it off in sync settings to push." | n/a (only fires user-initiated) |
| GitHub App not installed / suspended / repo unauth | `S.APP_NOT_INSTALLED` / `S.APP_SUSPENDED` / `S.REPO_NOT_AUTHORIZED` | (browser-open `params['url']`) | `[auto-sync]` (silent) |
| Auth refresh window closing | `S.AUTH_REFRESH_STALE` | "GitHub session needs re-authentication — current access expires {deadline}. Open GitHub Connect and tap Re-authenticate." | `[auto-sync]` (banner) |
| Daemon transport down | `S.SERVER_UNAVAILABLE` | "Sync service unavailable: {error}" | `[auto-sync]` + `[server-crash]` |
| Daemon raised | `S.SERVER_ERROR` | "Sync service error: {error}" | `[auto-sync]` (silent) |
| DNS resolution failed | `S.DNS_RESOLUTION_FAILED` | "Network reachable, but the sync host could not be resolved. Sync will retry automatically when this clears. If it persists, check this device's Private DNS, VPN, or per-app data restrictions." | `[auto-sync]` (silent) |
| In-process retries exhausted | `S.SYNC_GIVING_UP_TRANSIENT` | "Sync gave up after {budget_s}s on a flaky network. {commits_pending} commit(s) still pending — they will go out on the next sync attempt." | `[auto-sync]` (silent) |
| Sync job interrupted mid-flight | `S.JOB_INTERRUPTED` | "Sync interrupted, please try again." (after retry) | `[auto-sync]` retry-once-silent |
| Daemon `project_lock` held | `S.BUSY` | (silent both paths) | `[auto-sync]` |
| Memory below merge threshold | `S.INSUFFICIENT_MEMORY_FOR_MERGE` | "Not enough memory to merge right now ({mem_available_mb} MB available, {min_required_mb} MB needed). Close other apps and the next sync will retry." | `[auto-sync]` (silent) |
| Same `device_name` collided with peer | `S.TOPIC_BRANCH_CONFLICT` | "Another device is using the same device name and our staging branch ({topic_branch}) collided with theirs (server tip {server_tip}). Change this device's name in the daemon settings to something unique and try again." | `[auto-sync]` (silent) |
| Push pack > network budget | `S.COMMIT_PACK_EXCEEDS_NETWORK_BUDGET` | "Could not push to GitHub: the server kept rejecting our push attempts (single commit {commit_sha}, {raw_bytes:,} bytes). This may be a connection problem or a GitHub-side issue — try again later or on a different network." | `[auto-sync]` (silent) |
| Suspiciously large audio committed | `S.LARGE_AUDIO_FILE_DETECTED` | "Unusually large file recorded: {path} ({bytes:,} bytes). The recorder is for word-list elicitation — please check whether this was a recording mistake." | `[auto-sync]` (banner — surfaced both paths) |
| Commit step failed | `S.COMMIT_FAILED` | "Commit: {error}" | `[auto-sync]` |
| Commit ≥2 in a row | `S.COMMIT_REPEATEDLY_FAILED` | "Saving to git has failed {count} times in a row ({error}). Your recordings are still on the device but aren't being backed up. Please enable Settings → Diagnostic log → Log server activity = yes, then Share daemon log so we can investigate." | **never silenced** |
| Files written not entering git | `S.DATA_LOSS_RISK` | "Data-loss risk: {count} file(s) written to your project aren't being backed up. Please enable Settings → Diagnostic log → Log server activity = yes, then Share daemon log so we can investigate." | **never silenced** |
| Push step failed | `S.PUSH_FAILED` | "Push failed: {error}" | `[auto-sync]` |

## Clone

| Situation | Code | User-facing | Log |
|---|---|---|---|
| Clone needs GH auth | `S.CLONE_AUTH_REQUIRED` | "Clone failed — repository not found. This may be a private repository.\n\nAre you authenticated to {host}?" | `[clone]` |
| Clone transport raised (unstructured) | — | — (silent) | `[clone]` |

## Slot / wordlist split (§ 21)

| Situation | Code | User-facing | Log |
|---|---|---|---|
| `team_size` shrunk below this device's slot | — (peer-detected) | "The team size changed. Pick your new recording slot." | `[split] stale slot ... exceeds new team_size` |
| Contributor unset when claiming | `S.CONTRIBUTOR_UNSET` | "Set your name in Sync Settings before claiming a recording slot. Tap 'Open Sync Settings' to continue." | — |
| `claim_slot` RPC returned False | — | — (silent; relies on next-sync re-fire) | `[split] claim_slot(...) returned False` |
| `list_slots` worker raised | — | — | `[split] worker list_slots failed` |
| `project_kv_get('team_size')` raised | — | — | `[split] worker project_kv_get failed` |
| `lan_peer_id()` empty → no slot match | — | — (currently swallows [k/n] — see open NOTE_TO_DAEMON) | `[split] no slot matched` |
| `release_stale_slot` worker raised | — | — | `[split] release of stale slot raised` |

## LIFT / project load

| Situation | Code | User-facing | Log |
|---|---|---|---|
| `.lift` open / parse failed | — | "Could not open file:\n{error}" (popup) | — |
| Template cleanup failed mid-load | — | — | `[load_lift] clean_template failed` |
| Orphan-audio bind raised on load | — | — | `[load_lift] orphan-audio bind raised` / `[reload] orphan-audio bind raised` |
| LIFT namespace scan failed | — | — | `lift namespace scan failed` (lift.py:41) |
| `_register_current_project` raised | — | — | `[register-project]` |
| `last_project()` raised on resume | — | — (current view preserved) | `[on_resume] last_project() failed` |

## CAWL prefetch / image cache

| Situation | Code | User-facing | Log |
|---|---|---|---|
| `cawl_index` RPC failed | — | — | `[cawl] cawl_index(...) failed` |
| Langcode lookup failed inside resolver | — | — | `[cawl] langcode lookup failed` |
| CAWL binary pull FileNotFoundError | — | — (no-image render) | `[cawl] _pull: FileNotFoundError` |
| CAWL binary pull ValueError (basename rejected) | — | — | `[cawl] _pull: ValueError` |
| CAWL binary pull generic Exception | — | — | `[cawl] _pull: {basename} failed` |
| 10 consecutive pull failures → circuit breaker | — | — (suppresses further pulls this session) | `[cawl] circuit breaker tripped` |
| `cawl_prefetch` RPC failed | — | — | `[image-prefetch] cawl_prefetch failed` |
| `all_cawl_paths` raised | — | — | `[image-prefetch] all_cawl_paths failed` |
| Cache-status poll raised | — | — | `[cache-status] poll failed` |

## Images (picker / save / search)

| Situation | Code | User-facing | Log |
|---|---|---|---|
| openclipart search empty | — | "No images found on openclipart" | `[openclipart]` |
| FreeSVG search empty | — | "No images found on FreeSVG" | `[freesvg]` |
| Wikimedia search empty | — | "No public domain images on Wikimedia" | `[wikimedia]` |
| Image search HTTP raised | — | — | `[openclipart]`/`[freesvg]`/`[wikimedia]` |
| Image pick → save successful (informational) | — | "Image updated" | — |
| PIL serialize raised | — | — | `[image-save] PIL serialize failed` |
| URI MediaHandle write raised | — | — | `[image-save] URI write failed` |
| Filesystem image write raised | — | — | `[image-save]` |
| `set_illustration` LIFT write raised | — | — | `[image-save]` |
| Picker activity-result raised | — | — | `[activity-result]` |
| URI → bitmap decode raised | — | — | `[uri-to-image]` |

## Startup migrations

| Situation | Code | User-facing | Log |
|---|---|---|---|
| `prefs.json` → `config.json` raised | — | — | `[migrate]` |
| Legacy `collab_name` → daemon `contributor` (worker) | — | — | `[migrate] collab_name -> contributor failed` |
| `migrate_from_prefs` credentials worker raised | — | — | `[migrate] credentials` |

## Collab / backup gating

| Situation | Code | User-facing | Log |
|---|---|---|---|
| User taps a collab-requiring action without setup | — | "You need to set up collaboration to do this." | — |
| Project loaded with no git remote | — | "Your data isn't being backed up! Please set up collaboration, so your data can be backed up automatically while you work." | — |

## ContentObserver (§ 17b)

| Situation | Code | User-facing | Log |
|---|---|---|---|
| `subscribe_project_changes` raised | — | — (silent — polling floor covers) | `[content-observer] subscribe failed` |
| `unsubscribe` raised | — | — | `[content-observer] unsubscribe failed` |

## Daemon health mirroring

| Situation | Code | User-facing | Log |
|---|---|---|---|
| `/v1/health` unreachable | — | — | `[server-crash] /v1/health unreachable — daemon is fully down` |
| Daemon `last_crash` present | — | — (logged verbatim into peer log) | `[server-crash]` |

---

**Never silenced** (surface on auto-sync AND user-Sync):
`S.DATA_LOSS_RISK`, `S.COMMIT_REPEATEDLY_FAILED`,
`S.LARGE_AUDIO_FILE_DETECTED`. Everything else marked "silent"
only surfaces on user-initiated sync per
`CLIENT_INTEGRATION.md` § 17 routing.
