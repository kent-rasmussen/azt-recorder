# Sync-status indicator — what each label means

This is exhaustive for the text shown between the sync icon
(left) and the gear / settings icon (right) on the recorder
top bar. The label is set by `_sync_status_info` (main.py:6009)
and is the only place this real estate gets touched at runtime.

This document tracks the **v0.47.0 rendering model**
(CLIENT_INTEGRATION.md § 17b). The previous LANOK / `+N` /
`+u/N` model from pre-v0.47.0 builds is gone; if you're
reading older recorder logs or screenshots that show
`(LANOK +3) · LAN-only`, that's the historical scheme.

The full label has up to three parts, concatenated in order:

```
{label} {badge?}{suffix?}
```

- **label** — one of five states: `OK` / `WAN-{n}` /
  `LAN-{n}` / `WAN-{w}_LAN-{l}` / `WAN-{w} LAN-{l}`.
  Driven by three independent ProjectStatus counts
  (`wan_unshared` / `lan_unshared` / `at_risk`).
- **badge** — `+{n}` in red when `n_changes > 0`
  (uncommitted working-tree files). Omitted otherwise.
  Always red regardless of toggle state.
- **suffix** — only ` · offline` ever surfaces. Implies
  `work_offline=ON` AND `lan_allow_sync=OFF`. Other
  toggle states render without a suffix.

Tapping the label fires `app.do_sync()` (a user-initiated
sync per CLIENT_INTEGRATION.md § 17). When `last_sync == 0`
the tap routes to the daemon's collab settings instead,
where Publish lives. Empty label → tap still works.

---

## 1. The five state labels

`wan = wan_unshared` (commits not on github).
`lan = lan_unshared` (commits not on any paired LAN peer's
last-seen main; 0 when no peers are paired).
`at_risk` (commits on neither channel — set intersection
of the two above).

| Condition | Label | Meaning |
|---|---|---|
| `wan == 0`, `lan == 0` | `OK` | Every local commit is on github AND on at least one paired LAN peer (or no LAN peers are configured). Fully durable. |
| `wan > 0`, `lan == 0` | `WAN-{wan}` | All local commits are on a LAN peer; some aren't on github yet. |
| `wan == 0`, `lan > 0` | `LAN-{lan}` | All local commits are on github; some haven't been LAN-delivered to one or more paired peers yet. |
| `wan > 0`, `lan > 0`, `at_risk == 0` | `WAN-{wan}_LAN-{lan}` | **Split-brain (rare):** different commits on each channel with no overlap. Requires divergent history (each device made commits the other doesn't have). Underscore separator. |
| `wan > 0`, `lan > 0`, `at_risk > 0` | `WAN-{wan} LAN-{lan}` | **Routine transient:** both channels behind on the same `at_risk` commits, normal state right after a fresh commit. Drops to WAN-N or LAN-N as one channel catches up. Space separator. |

Frequency in normal workflow: `OK` > `WAN-N` / `LAN-N` >
both-behind > split-brain.

## 2. Per-channel red coloring

The rule is "settings allow this to be stored, but it
isn't stored yet." Transient red = normal automation in
flight; persistent red = something's broken; **black** =
"settings preclude this resolution; you accepted it by
design (phone in the forest)."

| Part | Red when | Black when |
|---|---|---|
| `WAN-{wan}` | `work_offline == False` (we should be pushing) | `work_offline == True` (you opted out) |
| `LAN-{lan}` | `lan_allow_sync == True` (LAN is armed) | `lan_allow_sync == False` (LAN listener disarmed) |
| `+{n_changes}` | always | n/a |

In compound labels (`WAN-x_LAN-y` / `WAN-x LAN-y`), each
part is colored independently per the same rule. The
separator (underscore or space) inherits the Label's
default color — not red.

## 3. Uncommitted-changes badge `+{n}`

`n_changes` is the daemon's count of working-tree files
modified but not yet committed. The badge renders as a
separate visual element next to the label, drawn in red:

| Rendered | Meaning |
|---|---|
| _(no badge)_ | `n_changes == 0`; the working tree is clean. |
| `+5` (red) | 5 dirty files awaiting commit. Short flashes during normal record/commit cycles are expected; a steady-red `+N` over multiple polls is the "auto-commit isn't draining" signal worth investigating. |

## 4. Suffix `· offline`

Only one combination of the two daemon-wide toggles
surfaces a suffix:

| `work_offline` | `lan_allow_sync` | Suffix | Mode |
|---|---|---|---|
| off | off | _(none)_ | Default. GitHub push runs whenever connectivity allows. |
| off | on | _(none)_ | Default + LAN delivery; both push paths active. |
| **on** | **off** | ` · offline` | **"Phone in the forest."** User suspended pushing AND LAN sharing. Commits accumulate; nothing leaves the device. |
| on | on | _(none)_ | LAN-only mode. User suspended GitHub push but kept LAN sharing on. Paired phones still receive. (Implied — no suffix.) |

The two `_(none)_` rows for the asymmetric toggles
(off+on, on+on) are intentional. The mode is visible to
the user elsewhere in the UI; surfacing it alongside every
status would be noise.

## 5. Empty label

`sync_status_label.text` is set to `''` when:

- No project is loaded (`_current_langcode` is empty).
- The server is unreachable for the `project_status` call
  (`project_status` returns `None`). The label silently
  blanks rather than showing a transient-error toast —
  per § 4 of the contract, the bootstrap popup owns
  "server missing" state.

## 6. Combined examples

Every full label is `{label}[ {badge}][{suffix}]` with each
part colored independently per the rules above.

| Rendered | What it tells the user |
|---|---|
| _(empty)_ | No project loaded, or daemon unreachable. |
| `OK` | Fully durable. Every commit is on github AND on at least one paired LAN peer (or no LAN peers configured). |
| `OK +5` | All commits durable; 5 dirty files in the working tree awaiting auto-commit. |
| `WAN-3` | 3 commits not on github (but all on LAN). `WAN-3` is red because `work_offline=off` (you should be pushing). Auto-push will catch up on the next drain. |
| `WAN-3` (black) | 3 commits not on github (but all on LAN). `WAN-3` is black because `work_offline=on` — you opted out of GitHub backup; the count is informational, not an alarm. |
| `LAN-2` (red) | 2 commits not on a paired LAN peer (but on github). LAN is armed; the listener will catch up. |
| `LAN-2` (black) | 2 commits not on a paired LAN peer; LAN listener is disarmed. You opted out of LAN delivery. |
| `WAN-3 LAN-2` | Both behind on the same 2 commits (at_risk=2). Plus 1 commit unique to WAN. Routine transient — drops to `WAN-1` once LAN catches up. |
| `WAN-3_LAN-2` | Split-brain: 3 commits on this device aren't on github; 2 different commits exist on a LAN peer but not on this device. Divergent history. |
| `WAN-3 +5` | 3 unpushed commits + 5 dirty files. The combination is the strongest "things aren't draining" signal. |
| `WAN-3 · offline` | 3 commits accumulating; work-offline mode on, LAN-share off. Nothing is leaving the device until you flip work_offline back off. |
| `OK · offline` | Phone-in-the-forest with everything caught up to the moment you flipped the toggle. Nothing pending. |

## 7. Status codes vs label text

This indicator is **pull-only state**, not a status-code
projection. It reads `ProjectStatus` and renders the
snapshot. Status codes (`S.NOT_A_REPO`, `S.AUTH_REQUIRED`,
`S.WORK_OFFLINE_ENABLED`, etc.) flow through a separate
channel — sync-gesture toasts and route-to-settings
dispatch per CLIENT_INTEGRATION.md § 17 — and never write
into `sync_status_label` directly.

The pre-v0.47.0 model had label prefixes ("not backed up",
"sign in to back up", "publish to back up", "add name to
back up") that shadowed the gesture-driven status codes.
The v0.47.0 model dropped those — when the user has setup
work to do, the WAN-N count surfaces it (a project with
no github remote walks the whole local history into
`wan_unshared`, intentional friction). The "what's the
next blocker" hint is conveyed by where the do_sync tap
routes (collab settings if `last_sync == 0`), not by the
badge text itself.

## 8. Refresh cadence

`_update_sync_status` runs:

- Every 10 s on a `Clock.schedule_interval` while the
  recorder screen is foreground.
- Immediately on `on_resume` (CLIENT_INTEGRATION.md
  § 14a).
- After every sync gesture's result handler
  (CLIENT_INTEGRATION.md § 17b "Background refresh
  obligation").
- After a project load, a project switch, and certain
  entry edits that may have triggered a debounced commit.

The 10 s cadence is the load-bearing one for catching
daemon-driven changes (background push completes,
`work_offline` toggled from another peer, LAN delivery
catches up). Content-reload (re-parsing LIFT after a
HEAD advance) is gated separately on `head_sha` change
per § 17b's background-refresh recipe — see
`_update_sync_status`'s content-advance block.
