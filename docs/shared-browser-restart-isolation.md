# Shared browser killed by Pi service restarts: own systemd unit, attach-only exit

## Incident (2026-10-13, Asia/Taipei)

At 00:35:00.611 another agent session ran `systemctl --user restart piweb-worker`
(changing PiWeb's default model; a timer it scheduled restarted the service again at
00:38:43). The shared Chrome died in the same second. The worker serving it was an
attach-only worker (`PI_NODRIVER_ATTACH_PORT`), started the previous evening by the
operator reconnect helper in its own `nodriver-browser-worker.service`. That worker
survived the restart, kept the shared socket and answered every request with a
failure, so Pi never started a fresh browser. Two research calls at 00:46 and 00:47
returned after 30.7 s with zero crawled pages and no Google results. Stopping that
attach-only worker restored service (the next call started a normal worker), but the
stop itself waited for systemd's 90 s timeout and SIGKILL.

## Root cause (confirmed by reproduction)

The worker is shared by every Pi process through one socket, but it was started as a
plain child of whichever Pi process needed it first, so it lived in that process's
systemd unit. Chrome then split itself across two cgroups:

| Process | cgroup |
|---|---|
| Chrome browser process | its own `app-com.google.Chrome-<pid>.scope` (Chrome moves only itself) |
| Zygotes, GPU, network/storage utilities, every renderer, crashpad | the unit of the Pi process that started the worker (e.g. `piweb-worker.service`) |
| `xvfb-run`, Xvfb, `worker.py` | same unit |

With the default `KillMode=control-group`, stopping that unit sends SIGTERM to every
process in it within about a millisecond. Two independent paths each kill Chrome:

- **Display gone**: with only Xvfb killed, the browser process exits 0 about 1.4 ms later.
- **Helpers gone**: with only the helpers killed and Xvfb alive, Chrome fails to relaunch
  its GPU process and aborts (`FATAL ... GPU process isn't usable. Goodbye.`).

In the previous layout the worker died too, so the next browser call quietly started a
new browser: every PiWeb restart (44 between 2026-10-06 and 2026-10-13) closed all open
tabs of all conversations without anyone noticing. The attach-only worker turned that
silent restart into an outage: it was outside the restarted unit, so it survived; its
socket kept answering, so Pi never respawned; and each crawl waited about 2.76 s for
nodriver's connection attempt to a dead port, serialized behind the browser lifecycle
lock, until research's 30 s callback deadline expired.

A separate defect made every worker stop slow: since Python 3.12.1,
`asyncio.Server.wait_closed()` (also called when leaving `async with server`) waits for
all connected clients, and each Pi process keeps one idle connection open. SIGTERM
therefore blocked until systemd escalated to SIGKILL.

Ruled out: the reduced environment of the first reproduction (the full 61-variable
production environment gave identical results), and Chrome stderr back-pressure
(nodriver pipes Chrome's output; on the live browser the pipes held 0 bytes and the
wrapper's `cat` had written nothing after 1.5 h). The exact path that killed Chrome at
00:35 was not traced live; both paths above reproduce deterministically.

### Reproduction

Isolated transient units, profiles, sockets and X displays; production units and the
shared Chrome were not touched. Signals and exit codes were traced with bpftrace.

| Experiment | Action | Result |
|---|---|---|
| E1 | Stop the unit that spawned the worker (legacy layout) | Xvfb exit at +9.9 ms, Chrome `exit(0)` at +10.1 ms; worker dies too |
| E2a | Kill only Xvfb | Chrome `exit(0)` 1.4 ms later |
| E2b | Kill only Chrome helpers, keep Xvfb | Chrome aborts after about 84 ms (GPU relaunch fails) |
| E3 | Attach-only replacement, then stop the spawning unit | Chrome dies at +11.4 ms; attach-only unit stays `active`; every request fails after 2.76 s |
| E5 | E3 with the full production environment, replaying the incident's four research queries | Healthy: 2.35–3.18 s, 12–14 pages. After Chrome death: 30.37–30.85 s, 0 pages (incident: 30.66 s, 0 pages); errors: 9 `crawl_empty_or_failed`, 5 `crawl:callback_timeout`, search exception |

## Fix

1. **Own unit for the browser** (`worker-launch.ts`, used by `index.ts`). On Linux, when
   the user systemd manager outlives the calling Pi process (Pi itself runs under it, as
   PiWeb does, or the user lingers), the worker starts as a transient unit
   `pi-nodriver-browser-<12 hex>.service` via `systemd-run --user --collect`
   (`TimeoutStopSec=20`). Xvfb and all Chrome helpers then live in that unit, so
   restarting PiWeb or any other Pi host no longer touches them.
   - The caller's environment is forwarded, minus variables systemd sets per unit,
     display variables (xvfb-run provides them) and names systemd rejects. Variables are
     passed as `--setenv=NAME`; systemd-run copies the value from its own environment, so
     secrets never appear in argv. The unit starts from the user manager's environment,
     so variables present only there (for example from `systemctl --user
     import-environment`) also reach the worker, unlike with the legacy child; forwarded
     values take precedence.
   - `$` in paths is escaped for ExecStart expansion, which substitutes `${VAR}` anywhere
     in an argument and `$VAR` as a whole word.
   - If `systemd-run` fails (including a timeout after the unit may already exist), the
     unit name is stopped before the fallback starts, so two workers never race for the
     socket lock.
   - Each start uses a fresh unit name, so the existing recovery (a new worker takes the
     socket lock from an unresponsive one) works unchanged and never collides with a
     lingering unit.
   - Without a user manager (containers, CI, other platforms), from a login session
     (SSH, terminal tmux) of a user who does not linger, or with `PI_NODRIVER_SYSTEMD=0`,
     the previous detached child process is used. Without lingering, logind stops the user
     manager at the last logout, which would stop the browser unit while Pi keeps running;
     the detached child survives that.
2. **Attach-only workers never outlive their Chrome** (`worker.py`).
   - Startup: if nothing listens on the attach port, exit 0 without taking the socket. An
     invalid `PI_NODRIVER_ATTACH_PORT` exits 2 with a one-line message instead of a
     traceback.
   - Watchdog: two consecutive refused connections to the CDP port, 2 s apart, make the
     worker exit 0 (no `Restart=on-failure` loop), closing all clients and removing the
     socket, so Pi's next call starts a fresh browser.
   - A browser command that finds the port refused fails immediately with an explicit
     "shared Chrome ... has exited" error and triggers the exit.
   - Only `ECONNREFUSED` counts: a busy or hung Chrome still completes the TCP
     handshake, so slowness never triggers an exit.
3. **Prompt shutdown** (`worker.py`): on exit the server waits at most 2 s for in-flight
   responses, then closes all client connections before a 5 s-bounded `wait_closed()`.
   Research cleanup is bounded to 5 s, browser shutdown to 10 s (`Browser.close` inside
   it to 5 s), so a wedged job cannot hold the exit until systemd's SIGKILL.
4. **Operator helper** (`ops/browser-worker-reconnect.py`): refuses a worker that runs
   in its own browser unit. There the `xvfb-run` wrapper is the unit's main process;
   killing it would end the unit and systemd would stop Xvfb and Chrome, the opposite of
   the helper's promise. It also forwards `RESEARCH_*` variables to the attach-only
   worker.

## Verification

| Check | Result |
|---|---|
| `tests/test_worker_lifecycle.py` (5) | The first four failed before the fix exactly as in the incident (no exit after SIGTERM, no exit without Chrome, no exit after Chrome loss, nodriver's 2.76 s connect error); the fifth (invalid attach port) failed with a traceback and exit 1. All pass after, repeatedly |
| `tests/worker-launch.test.ts` (10) | Environment filtering, names-only argv, `$` escaping, unique units, fallbacks, stopping a possibly created unit before the fallback, user-manager durability (service context, lingering, login session) |
| `tests/test_worker_reconnect.py` (2) | Dedicated-unit detection for the operator helper |
| `tests/test_shared_browser_attach.py` (+1) | Attaching to an exited Chrome fails fast, never calls nodriver's connect, and requests the worker exit; the existing attach test now uses its own listener instead of a fixed production port |
| Full offline suite (clean snapshots, same environment) | Baseline 731 tests, 90 skipped, OK; candidate 740 tests, 91 skipped (the opt-in systemd test), OK. TypeScript: 53 tests pass |
| `tests/test_systemd_browser_unit.py` (opt-in, real Chrome) | Stopping the starting unit leaves worker and Chrome alive and usable; stopping the browser unit with an idle client finishes within 10 s and removes everything. The socket path contains a literal `${HOME}`. Passes repeatedly. Negative controls fail as expected: `PI_NODRIVER_SYSTEMD=0` (worker in the starting unit) and a launcher without `$` escaping (socket created elsewhere) |
| E6: production environment, new launcher (final code) | Worker in `pi-nodriver-browser-*`, no Chrome helper in the starting unit. Stopping it (PiWeb restart stand-in): worker and Chrome alive; research 4.14 s after the tool call, 20 pages, 9 Google results (4.02 s before the stop) |
| E7: incident path with the new worker code (final code) | Chrome died 0.03 s after the trigger; the attach-only worker exited 0 after 2.86 s, closed its idle client, removed the socket and was not restarted. A fresh worker was listening 0.5 s later; research 3.24 s, 18 pages |

## Operations and behavior changes

- Find the browser: `systemctl --user list-units 'pi-nodriver-browser-*'`; logs with
  `journalctl --user -u <unit>`.
- **The browser now outlives Pi and PiWeb restarts.** Updated worker code takes effect
  only after the browser restarts: `systemctl --user stop 'pi-nodriver-browser-*'`
  (profile and cookies are kept, open tabs are not); `install.sh` already sends the
  worker a shutdown.
- **OOM priority changed.** The legacy browser inherited `oom_score_adj` −1000 from the
  protected PiWeb cgroup; the browser unit runs at the user manager default (200 on this
  host), so under memory pressure Chrome is now a preferred victim. A killed browser is
  relaunched by the worker on the next call.
- **Transition.** Pi processes started before an update keep the old launcher in memory,
  and a worker that is already running keeps running. The first restart of such a host
  still takes the old browser down once; the next call then starts the browser unit.
- **Worker-only replacement is a legacy-layout tool.** For a browser unit, restart the
  whole unit instead; the reconnect helper refuses and prints the command.

## Limitations

- Chrome now runs for days instead of hours; long-uptime memory growth has not been
  measured (the per-session tab limit still applies).
- The forwarded environment is stored in the transient unit under
  `/run/user/<uid>/systemd/transient/` (user-private), the same exposure class as the
  worker's `/proc/<pid>/environ`.
- If Xvfb dies inside a browser unit for another reason, the worker keeps relaunching
  Chrome on a dead display until the unit is restarted (pre-existing, not addressed).
- Fallback environments keep the previous layout and its exposure to host restarts.
