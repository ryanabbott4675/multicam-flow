#!/usr/bin/env python3
"""Multicam Flow one-click pipeline: synced moments -> multicam XMLs -> (optionally) cut -> clean output.

python3 pipeline.py <sync.json> --selection sel.json [--cut] [--out-root ~/Movies/Multicam Flow]

Working files (caches, logs, review sheets) live in Multicam Flow/Work/<Couple>/.
Finished XMLs land in <out-root>/<Couple>/:
    Ceremony - Sarah and James.fcpxml          (cut, ready for Final Cut)
    Synced only/Ceremony - Sarah and James.fcpxml   (multicam + project, no cut)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "engine.py")
# Working files (caches, logs, results). The Mac app points this at ~/Library/Application Support.
DATA = os.environ.get("MCF_DATA") or HERE
os.makedirs(DATA, exist_ok=True)
sys.path.insert(0, HERE)


def say(*a):
    print(*a, flush=True)


def safe(name):
    return re.sub(r'[/:\\]+', "-", name).strip() or "Moment"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sync_json")
    ap.add_argument("--selection", required=True, help="JSON file or string: moments to build")
    ap.add_argument("--cut", action="store_true", help="cut each multicam after syncing")
    ap.add_argument("--out-root", default=os.path.expanduser("~/Movies/Multicam Flow"))
    ap.add_argument("--work-root", default=os.path.join(DATA, "Work"))
    args = ap.parse_args()

    import ingest
    sel = args.selection
    sel = json.load(open(sel)) if os.path.exists(sel) else json.loads(sel)
    data = json.load(open(args.sync_json))
    couple = safe(data.get("couple") or "Wedding")
    work = os.path.join(args.work_root, couple)
    synced_dir = os.path.join(work, "_synced")
    out_dir = os.path.join(os.path.expanduser(args.out_root), couple)
    os.makedirs(synced_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    say("@stage 0 1 Building synced multicams")
    ingest.cmd_build(args.sync_json, dest=synced_dir, selection=sel)
    man = json.load(open(os.path.join(synced_dir, "manifest.json")))
    secs = man["sections"]
    results, warnings = [], []
    total = sum(max(1.0, s["duration"]) for s in secs) or 1.0
    done_w = 0.0

    for k, s in enumerate(secs):
        name = s["name"]
        base = f"{name} - {couple}"
        sdir = os.path.join(work, safe(name))
        os.makedirs(sdir, exist_ok=True)
        src = os.path.join(sdir, base + ".fcpxml")
        shutil.copy2(s["path"], src)
        synced_out = os.path.join(out_dir, "Synced only" if args.cut else "", base + ".fcpxml")
        os.makedirs(os.path.dirname(synced_out), exist_ok=True)
        shutil.copy2(s["path"], synced_out)
        entry = {"name": name, "style": s["style"], "synced": synced_out, "cut": None,
                 "minutes": round(s["duration"] / 60, 1), "audio": s["audio"]}
        if args.cut:
            say(f"@stage {k + 1} {len(secs)} Cutting {name}")
            cmd = [sys.executable, "-u", ENGINE, src, "--out", sdir, "--main", "none", "--audio", "timeline",
                   "--remove-gaps", "--style", s["style"]]
            if s.get("roles"):
                cmd += ["--roles", s["roles"]]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            prog, tail = {}, []
            w = max(1.0, s["duration"]) / total
            for line in p.stdout:
                line = line.rstrip("\n")
                m = re.match(r"@progress (\w+) (.+) (\d+) (\d+)$", line)
                if m:
                    prog[f"{m.group(1)}:{m.group(2)}"] = int(m.group(3)) / max(1, int(m.group(4)))
                    vids = [v for kk, v in prog.items() if kk.startswith("video:")]
                    frac = 0.65 * (sum(vids) / len(vids) if vids else 0) + 0.33 * prog.get("transcript:all", 0)
                    say(f"@pct {min(0.99, done_w + w * frac):.3f}")
                    continue
                tail.append(line)
                if "could not decode" in line or "missing media" in line:
                    warnings.append(f"{name}: {line.strip()}")
                if line.strip() and not line.startswith((" ", "{", "}", "]", '  "')):
                    say(line)
            rc = p.wait()
            out_x = os.path.join(sdir, base + " Multicam Flow.fcpxml")
            summ = os.path.join(sdir, "summary.json")
            if rc == 0 and os.path.exists(out_x):
                final = os.path.join(out_dir, base + ".fcpxml")
                shutil.copy2(out_x, final)
                entry["cut"] = final
                try:
                    sm = json.load(open(summ))
                    entry.update(cuts=sm.get("cuts"), avg_shot=sm.get("avg_shot_sec"), length=sm.get("length"),
                                 gaps=round(sm.get("inplace_stats", {}).get("gap_seconds", 0) / 60, 1))
                except Exception:
                    pass
            else:
                warnings.append(f"{name}: the cut failed. " + " | ".join(t for t in tail[-6:] if t.strip()))
            done_w += w
        results.append(entry)
        say(f"@pct {min(0.99, done_w if args.cut else (k + 1) / len(secs)):.3f}")

    res = {"couple": couple, "out_dir": out_dir, "work": work, "cut": args.cut,
           "moments": results, "warnings": warnings, "finished": time.time()}
    json.dump(res, open(os.path.join(work, "result.json"), "w"), indent=1)
    say("@result " + json.dumps(res))


if __name__ == "__main__":
    main()
