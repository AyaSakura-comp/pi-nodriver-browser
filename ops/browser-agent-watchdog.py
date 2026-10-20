#!/usr/bin/env python3
"""Watchdog for the services browser_intent depends on.

Run once per minute (browser-agent-watchdog.timer). Each run health-checks every service,
revives the dead ones, and appends incidents to events.jsonl so outages can be analysed later:

    browser-agent-watchdog.py            # one check pass (what the timer runs)
    browser-agent-watchdog.py status     # current health, no actions
    browser-agent-watchdog.py report [--days 7]

The death time comes from systemd/journal (when the process actually exited), not from when
this check noticed, so a 60 s check interval does not blur the timeline. Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(os.environ.get("BROWSER_WATCHDOG_DIR", HOME / ".local/state/browser-agent-watchdog"))
EVENTS = STATE_DIR / "events.jsonl"
STATE = STATE_DIR / "state.json"
EXT = HOME / ".pi/agent/extensions/nodriver-browser"
WORKER_SOCKET = Path(os.environ.get("PI_NODRIVER_SOCKET", HOME / ".pi/agent/nodriver-browser.sock"))
WORKER_PREFIX = "pi-nodriver-browser-"

MAX_RESTARTS_PER_HOUR = 3
UNHEALTHY_RESTART_AFTER = 3     # consecutive failed checks while the process is still running
WORKER_REVIVE_AFTER = 2         # pi also auto-starts the worker on demand; don't race it

# revive: "always" = start when dead, restart when hung; "failed-only" = only after a crash
# (qwen-mtp is stopped on purpose when switching to Strata/Gemma, and a 3-4 min load looks "hung").
SERVICES = [
    {"name": "laya-intent", "scope": "user", "unit": "laya-intent.service",
     "health": "http://127.0.0.1:8011/health", "revive": "always", "ready_s": 40},
    {"name": "omniparser", "scope": "user", "unit": "omniparser.service",
     "health": "http://127.0.0.1:8012/health", "revive": "always", "ready_s": 40},
    {"name": "laya", "scope": "user", "unit": "laya.service",
     "health": "http://127.0.0.1:8000/health", "revive": "always", "ready_s": 90},
    {"name": "qwen-planner", "scope": "system", "unit": "qwen-mtp.service",
     "health": "http://127.0.0.1:8001/health", "revive": "failed-only", "ready_s": 0},
    {"name": "nodriver-worker", "scope": "user", "unit": None, "socket": str(WORKER_SOCKET),
     "revive": "always", "ready_s": 30},
]


def now() -> float:
    return time.time()


def iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


def run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def systemctl(scope: str, *args: str, sudo: bool = False) -> subprocess.CompletedProcess:
    cmd = (["sudo", "-n"] if sudo else []) + ["systemctl"] + (["--user"] if scope == "user" else []) + list(args)
    return run(cmd, timeout=30)


def unit_props(scope: str, unit: str) -> dict:
    keys = "ActiveState,SubState,Result,MainPID,ExecMainPID,ExecMainCode,ExecMainStatus,InvocationID," \
           "InactiveEnterTimestamp,ActiveEnterTimestamp,NRestarts,LoadState"
    out = systemctl(scope, "show", unit, "-p", keys, "--timestamp=unix").stdout
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    for k in ("InactiveEnterTimestamp", "ActiveEnterTimestamp"):
        v = props.get(k, "").lstrip("@")
        props[k] = float(v) if v.replace(".", "").isdigit() and float(v) > 0 else None
    return props


def http_ok(url: str, timeout: float = 5) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
            return r.status == 200, f"HTTP {r.status} {body[:120]}"
    except Exception as exc:  # refused, timeout, 503 while loading, ...
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def socket_ok(path: str, timeout: float = 2) -> tuple[bool, str]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
        return True, "socket accepts"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def worker_units() -> list[str]:
    out = systemctl("user", "list-units", f"{WORKER_PREFIX}*", "--all", "--plain", "--no-legend").stdout
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def memory() -> dict:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        if k in ("MemAvailable", "SwapFree"):
            info[k] = int(v.split()[0]) // 1024
    return {"mem_available_mb": info.get("MemAvailable"), "swap_free_mb": info.get("SwapFree")}


def journal(args: list[str], since: float) -> list[str]:
    try:
        return run(["journalctl", "--no-pager", "-o", "short-iso", "--since", f"@{int(since)}", *args]).stdout.splitlines()
    except Exception:
        return []


def find_cause(pid: str | None, since: float, props: dict | None, unit: str | None, scope: str) -> tuple[str, list[str]]:
    """Who killed it: earlyoom, the kernel OOM killer, a crash, or a clean stop."""
    evidence: list[str] = []
    if pid and pid != "0":
        hits = [l for l in journal(["-u", "earlyoom"], since) if re.search(rf"process {pid}\b", l)]
        if hits:
            sig = re.search(r"sending (SIG\w+)", " ".join(hits))
            return f"earlyoom-{sig.group(1) if sig else 'kill'}", hits[:3]
        hits = [l for l in journal(["-k"], since) if re.search(rf"Killed process {pid}\b", l)]
        if hits:
            return "kernel-oom", hits[:3]
    if unit:
        evidence = [l for l in journal((["--user"] if scope == "user" else []) + ["-u", unit], since)
                    if re.search(r"exited|Failed|Killed|killed|Stopping|Stopped|Deactivated|core|signal|Traceback|Error", l)][-6:]
    exited = next((m for m in (re.search(r"Main process exited, code=(\w+), status=([\w/]+)", l) for l in evidence) if m), None)
    if exited:
        return (f"signal-{exited.group(2).split('/')[-1]}" if exited.group(1) in ("killed", "dumped")
                else f"exit-{exited.group(2)}"), evidence
    if props:
        code, status, result = props.get("ExecMainCode", ""), props.get("ExecMainStatus", ""), props.get("Result", "")
        if result not in ("", "success"):
            return f"{result} (code={code} status={status})", evidence
        if props.get("ActiveState") == "inactive":
            return "clean-exit (manual stop, or SIGTERM handled as success)", evidence
    return "unknown", evidence


def death_time(props: dict | None, unit: str | None, scope: str, since: float) -> float | None:
    if props and props.get("InactiveEnterTimestamp") and props["InactiveEnterTimestamp"] >= since - 1:
        return props["InactiveEnterTimestamp"]
    if unit:  # transient units are garbage-collected; the journal still has the stop line
        try:
            out = run(["journalctl", "--no-pager", "-o", "json", "--since", f"@{int(since)}"]
                      + (["--user"] if scope == "user" else []) + ["-u", unit]).stdout.splitlines()
            for line in out:  # the first stop after the last healthy check is the death
                rec = json.loads(line)
                msg = rec.get("MESSAGE")
                if isinstance(msg, str) and re.search(
                        r"Deactivated|Failed with result|Main process exited|Stopped|Consumed .* CPU time", msg):
                    return int(rec["__REALTIME_TIMESTAMP"]) / 1e6
        except Exception:
            pass
    return None


def log_event(service: str, event: str, **fields) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": iso(now()), "service": service, "event": event, **fields}
    with EVENTS.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))


def start_worker() -> tuple[bool, str]:
    """Same launch as worker-launch.ts: its own transient unit, so it outlives this run."""
    unit = f"{WORKER_PREFIX}{os.urandom(6).hex()}"
    try:
        WORKER_SOCKET.unlink(missing_ok=True)
    except OSError:
        pass
    r = run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}",
             "--description=Pi nodriver browser (Xvfb, worker and Chrome; outlives the Pi process that started it)",
             f"--working-directory={EXT}", "--property=TimeoutStopSec=20", "--",
             "xvfb-run", "-a", "-s", "-screen 0 1366x768x24",
             str(EXT / ".venv/bin/python"), str(EXT / "worker.py"), "--server", str(WORKER_SOCKET)], timeout=20)
    return r.returncode == 0, unit if r.returncode == 0 else (r.stderr or r.stdout)[:200]


def main_pid(props: dict | None, unit: str | None) -> str | None:
    if props is None and unit:  # the worker's transient unit
        props = {"MainPID": systemctl("user", "show", unit, "-p", "MainPID", "--value").stdout.strip()}
    pid = (props or {}).get("MainPID")
    return pid if pid and pid != "0" else None


def check(svc: dict) -> tuple[bool, str, dict | None, str | None]:
    if svc["unit"] is None:
        units = worker_units()
        ok, detail = socket_ok(svc["socket"])
        return ok, detail, None, (units[0] if units else None)
    props = unit_props(svc["scope"], svc["unit"])
    if props.get("ActiveState") != "active":
        return False, f"unit {props.get('ActiveState')}/{props.get('SubState')}", props, svc["unit"]
    ok, detail = http_ok(svc["health"])
    return ok, detail, props, svc["unit"]


def revive(svc: dict, props: dict | None) -> tuple[bool, str]:
    if svc["unit"] is None:
        return start_worker()
    sudo = svc["scope"] == "system"
    if props and props.get("ActiveState") == "failed":
        systemctl(svc["scope"], "reset-failed", svc["unit"], sudo=sudo)
    r = systemctl(svc["scope"], "restart", svc["unit"], sudo=sudo)
    return r.returncode == 0, (r.stderr or "restart issued").strip()[:200]


def wait_healthy(svc: dict) -> bool:
    deadline = now() + svc["ready_s"]
    while now() < deadline:
        time.sleep(2)
        if check(svc)[0]:
            return True
    return False


def pass_once(states: dict) -> None:
    t = now()
    for svc in SERVICES:
        name = svc["name"]
        st = states.setdefault(name, {})
        last_check = st.get("last_check", t - 120)
        ok, detail, props, unit = check(svc)

        # systemd's own Restart= revived it between two checks: still an outage worth recording.
        inv = props.get("InvocationID") if props else unit
        if ok and st.get("invocation") and inv != st["invocation"] and not st.get("down_since"):
            cause, evidence = find_cause(st.get("pid"), last_check, None, svc["unit"] or st.get("unit"), svc["scope"])
            died = death_time(None, svc["unit"] or st.get("unit"), svc["scope"], last_check)
            back = props.get("ActiveEnterTimestamp") if props else None
            log_event(name, "restarted_outside_watchdog", previous_pid=st.get("pid"), cause=cause, died_at=iso(died),
                      came_back_at=iso(back), downtime_s=round(back - died) if died and back else None, evidence=evidence)

        if ok:
            if st.get("down_since"):
                log_event(name, "recovered", died_at=iso(st["down_since"]), detected_at=iso(st["detected_at"]),
                          downtime_s=round(t - st["down_since"]), by=st.get("revived_by", "itself"))
            states[name] = {"last_ok": t, "last_check": t, "invocation": inv, "unit": unit,
                            "pid": main_pid(props, unit), "restarts": st.get("restarts", [])}
            continue

        st["last_check"] = t
        st["fails"] = st.get("fails", 0) + 1
        dead = svc["unit"] is None or (props or {}).get("ActiveState") != "active"
        if not st.get("down_since"):
            since = st.get("last_ok", t - 3600)
            died = death_time(props, svc["unit"] or st.get("unit"), svc["scope"], since) if dead else None
            cause, evidence = (find_cause(st.get("pid"), since, props, svc["unit"] or st.get("unit"), svc["scope"])
                               if dead else ("unhealthy (process running, health check failing)", []))
            st.update(down_since=died or t, detected_at=t, cause=cause)
            log_event(name, "down", died_at=iso(died or t), died_at_source="systemd/journal" if died else "detected",
                      detected_at=iso(t), last_ok=iso(st.get("last_ok")), cause=cause, health=detail,
                      unit_state=f"{props.get('ActiveState')}/{props.get('SubState')}" if props else None,
                      result=(props or {}).get("Result"), exit=f"{(props or {}).get('ExecMainCode')}/{(props or {}).get('ExecMainStatus')}" if props else None,
                      pid=st.get("pid"), **memory(), evidence=evidence)

        if svc["revive"] == "failed-only" and (props or {}).get("ActiveState") != "failed":
            continue  # stopped on purpose or still loading: record only
        if not dead and st["fails"] < UNHEALTHY_RESTART_AFTER:
            continue
        if svc["unit"] is None and st["fails"] < WORKER_REVIVE_AFTER:
            continue
        recent = [r for r in st.get("restarts", []) if r > t - 3600]
        if len(recent) >= MAX_RESTARTS_PER_HOUR:
            if not st.get("gave_up"):
                st["gave_up"] = True
                log_event(name, "giveup", restarts_last_hour=len(recent), **memory(),
                          hint="restart budget spent; likely memory pressure (earlyoom) or a crash loop")
            continue
        okr, msg = revive(svc, props)
        st["restarts"] = recent + [t]
        st["revived_by"] = "watchdog"
        healthy = okr and wait_healthy(svc)
        log_event(name, "revive", ok=okr, healthy=healthy, detail=msg, **memory())
        if healthy:
            _, _, props2, unit2 = check(svc)
            log_event(name, "recovered", died_at=iso(st["down_since"]), detected_at=iso(st["detected_at"]),
                      downtime_s=round(now() - st["down_since"]), by="watchdog")
            states[name] = {"last_ok": now(), "last_check": now(), "unit": unit2, "pid": main_pid(props2, unit2),
                            "invocation": props2.get("InvocationID") if props2 else unit2,
                            "restarts": st["restarts"]}


def status() -> None:
    for svc in SERVICES:
        ok, detail, props, unit = check(svc)
        print(f"{'OK  ' if ok else 'DOWN'} {svc['name']:16} {unit or '-':40} {detail}")


def report(days: float) -> None:
    if not EVENTS.exists():
        print("no events yet")
        return
    cutoff = now() - days * 86400
    rows = [json.loads(l) for l in EVENTS.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if dt.datetime.fromisoformat(r["ts"]).timestamp() >= cutoff]
    by = collections.defaultdict(list)
    for r in rows:
        by[r["service"]].append(r)
    print(f"Browser-agent outages, last {days:g} day(s)")
    for name in [s["name"] for s in SERVICES]:
        ev = by.get(name, [])
        downs = [e for e in ev if e["event"] == "down"]
        outside = [e for e in ev if e["event"] == "restarted_outside_watchdog"]
        rec = [e for e in ev if e["event"] == "recovered"]
        causes = collections.Counter(e.get("cause", "?").split(" (")[0] for e in downs + outside)
        total = sum(e.get("downtime_s") or 0 for e in rec + outside)
        print(f"\n{name}: {len(downs)} outage(s), {len(outside)} restart(s) by systemd, downtime {total}s"
              + (f"; causes: {dict(causes)}" if causes else ""))
        for e in (downs + outside)[-10:]:
            print(f"  {e.get('died_at') or e.get('came_back_at') or e['ts']}  {e['event']:27} {e.get('cause', '')}")
    hours = collections.Counter(dt.datetime.fromisoformat(r.get("died_at") or r["ts"]).hour
                                for r in rows if r["event"] in ("down", "restarted_outside_watchdog"))
    if hours:
        print("\nby hour of day: " + "  ".join(f"{h:02d}h:{n}" for h, n in sorted(hours.items())))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="check", choices=["check", "status", "report"])
    ap.add_argument("--days", type=float, default=7)
    a = ap.parse_args()
    if a.cmd == "status":
        return status()
    if a.cmd == "report":
        return report(a.days)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        states = json.loads(STATE.read_text())
    except (OSError, ValueError):
        states = {}
    pass_once(states)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(states, indent=1))
    tmp.replace(STATE)


if __name__ == "__main__":
    sys.exit(main())
