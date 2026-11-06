#!/usr/bin/env python3
"""Compact JSON health snapshot of the obekt perp engine. Runs ON the VPS.
Read-only. Printed fields feed the Mac-side watchdog signature."""
import json, os, subprocess, time
from datetime import datetime

HERE = "/opt/trader/engine"
out = {}
now = time.time()
out["hour_bucket"] = int(now // 3600) % 24  # age proxy for re-fire on persistent DEAD/UNREACHABLE

try:
    out["timer_active"] = subprocess.run(
        ["systemctl", "is-active", "obekt-mom.timer"],
        capture_output=True, text=True, timeout=15).stdout.strip()
except Exception:
    out["timer_active"] = "err"

try:
    st = os.stat(os.path.join(HERE, "perp_log.jsonl"))
    out["log_age_min"] = round((now - st.st_mtime) / 60, 1)
except Exception:
    out["log_age_min"] = -1

stops = []
for f in ("STOP", "STOP_PERP"):
    p = os.path.join(HERE, f)
    if os.path.exists(p):
        try:
            age_h = int((now - os.stat(p).st_mtime) / 3600)
            head = open(p).read(60).replace("\n", " ")
        except Exception:
            age_h, head = -1, "?"
        stops.append({"f": f, "age_h": age_h, "head": head})
out["stops"] = stops

try:
    s = json.load(open(os.path.join(HERE, "perp_status.json")))
    out["equity"] = s.get("usdc_equity")
    out["n_pos"] = s.get("n_positions")
    out["halted"] = s.get("halted")
    out["engine"] = s.get("engine")
    ts = s.get("ts")
    if ts:
        out["status_age_min"] = round((now - datetime.fromisoformat(ts).timestamp()) / 60, 1)
except Exception as e:
    out["status_err"] = str(e)[:80]

try:
    cutoff = now - 3600
    errs = warns = jev = 0
    last_tokens = None
    with open(os.path.join(HERE, "perp_log.jsonl")) as fh:
        for line in fh:
            if "_err" not in line and "budget_warn" not in line and "perp_jev" not in line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            try:
                t = datetime.fromisoformat(e.get("ts", "")).timestamp()
            except Exception:
                continue
            if t < cutoff:
                continue
            ev = e.get("event", "")
            if ev.endswith("_err"):
                errs += 1
            elif "budget_warn" in ev:
                warns += 1
            elif ev == "perp_jev":
                jev += 1
                if e.get("input_tokens"):
                    last_tokens = e["input_tokens"]
    out["err_60m"] = errs
    out["budget_warn_60m"] = warns
    out["jev_60m"] = jev
    out["last_input_tokens"] = last_tokens
except Exception as e:
    out["log_err"] = str(e)[:80]

print(json.dumps(out))
