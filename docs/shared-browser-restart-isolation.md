# Shared browser killed by Pi service restarts: the worker's own systemd unit

## Incident (2026-10-13, Asia/Taipei)

At 00:35 another agent session ran `systemctl --user restart piweb-worker`. The shared
Chrome died in the same second, and two research calls a few minutes later returned
after 30 s with zero crawled pages and no Google results.

## Root cause (reproduced)

The worker is shared by every Pi process through one socket, but it was started as a
plain child of whichever Pi process needed it first, so it lived in that process's
systemd unit. Chrome moves only its own browser process into an `app-*.scope`; Xvfb,
`worker.py` and every Chrome helper (zygotes, GPU, network, renderers) stayed in the
starting unit (e.g. `piweb-worker.service`). Stopping that unit SIGTERMs all of them,
and either path kills Chrome: without Xvfb the browser process exits about 1.4 ms later;
without its helpers it fails to relaunch the GPU process and aborts. Before the incident
this went unnoticed because the worker died too and the next call started a new browser,
but every PiWeb restart (44 in one week) silently closed every conversation's tabs.

A second defect made every worker stop slow: since Python 3.12.1
`asyncio.Server.wait_closed()` waits for all connected clients, and each Pi process keeps
an idle connection open, so SIGTERM blocked until systemd's SIGKILL.

## Fix

- **Own unit for the browser** (`worker-launch.ts`). On Linux, when the user systemd
  manager outlives the calling Pi process (Pi runs under it, as PiWeb does, or the user
  lingers), the worker starts as a transient unit `pi-nodriver-browser-<12 hex>.service`
  via `systemd-run --user --collect` with `TimeoutStopSec=20`, so Xvfb and all Chrome
  helpers live in that unit and restarting any Pi host no longer touches them. The
  caller's environment is forwarded by name (`--setenv=NAME`, values never in argv),
  minus systemd's per-unit and display variables; `$` in paths is escaped for ExecStart
  expansion; a failed `systemd-run` stops the unit name before falling back, so two
  workers never race for the socket lock. Without a durable user manager (containers, CI,
  a login session of a user who does not linger) the worker starts as a detached child.
- **Prompt shutdown** (`worker.py`): on SIGTERM the server closes all client connections,
  bounds research cleanup to 5 s and browser shutdown to 15 s.

Verification: `tests/worker-launch.test.ts` (environment filtering, names-only argv, `$`
escaping, fallbacks) and the opt-in `tests/test_systemd_browser_unit.py` (real Chrome:
stopping the starting unit leaves worker and Chrome alive and usable; stopping the
browser unit with an idle client finishes within 10 s and removes everything; the socket
path contains a literal `${HOME}`).

## Operations

- Find the browser: `systemctl --user list-units 'pi-nodriver-browser-*'`; logs with
  `journalctl --user -u <unit>`.
- The browser outlives Pi and PiWeb restarts, so new worker code takes effect only after
  the worker restarts: `systemctl --user stop 'pi-nodriver-browser-*'` (profile and
  cookies are kept, open tabs are not); `install.sh` shuts the worker down.
- The browser unit runs at the user manager's default `oom_score_adj` (200 on this host),
  not PiWeb's protected −1000, so under memory pressure Chrome is a preferred victim; the
  worker relaunches a killed Chrome on the next call.

## Limitations

- If Xvfb dies inside a browser unit for another reason, the worker keeps relaunching
  Chrome on a dead display until the unit is restarted.
- The forwarded environment is stored in the transient unit under
  `/run/user/<uid>/systemd/transient/` (user-private), the same exposure as the worker's
  `/proc/<pid>/environ`.
