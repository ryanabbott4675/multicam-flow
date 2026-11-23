#!/usr/bin/env python3
"""Multicam Flow window. A small local web app (only reachable from this Mac).
Finds Final Cut exports automatically, asks main camera and audio, runs the
analysis with a progress bar, and shows the result."""

import json
from urllib.parse import quote, unquote
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
ENGINE = os.path.join(HERE, "engine.py")
# Working files (caches, logs, results). The Mac app points this at ~/Library/Application Support.
DATA = os.environ.get("MCF_DATA") or HERE
os.makedirs(DATA, exist_ok=True)
DONE = os.path.join(DATA, "Done")
INBOX = os.path.join(DATA, "Inbox")
PORT_FILE = os.path.join(DATA, ".ui_port")
ROLES_FILE = os.path.join(DATA, "camera_roles.json")


def load_roles():
    try:
        return json.load(open(ROLES_FILE))
    except Exception:
        return {}


def save_roles(uid, names_to_roles):
    allr = load_roles()
    allr[uid or "_last"] = names_to_roles
    allr["_last"] = names_to_roles
    try:
        json.dump(allr, open(ROLES_FILE, "w"), indent=1)
    except OSError:
        pass
HOME = os.path.expanduser("~")
SCAN = [INBOX, os.path.join(HOME, "Downloads"), os.path.join(HOME, "Desktop"),
        os.path.join(HOME, "Movies"), os.path.join(HOME, "Documents")]

JOB = {"running": False, "log": [], "progress": {}, "done": False, "ok": None,
       "summary": None, "name": None, "work": None, "started": None}
LOCK = threading.Lock()


def find_exports():
    seen, out = set(), []
    for root in SCAN:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("/").count("/")
        try:
            for dp, dn, fn in os.walk(root):
                if dp.count("/") - base_depth >= 3:
                    dn[:] = []
                keep = []
                for d in dn:
                    full = os.path.join(dp, d)
                    if d.endswith(".fcpxmld"):
                        out.append(full)
                    elif not d.startswith(".") and not d.endswith((".fcpbundle", ".app", ".photoslibrary")) \
                            and full != DONE:
                        keep.append(d)
                dn[:] = keep
                for f in fn:
                    if f.endswith(".fcpxml") and "Multicam Flow" not in f:
                        out.append(os.path.join(dp, f))
        except PermissionError:
            continue
    res = []
    for p in out:
        rp = os.path.realpath(p)
        if rp in seen:
            continue
        seen.add(rp)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        res.append({"path": p, "name": os.path.splitext(os.path.basename(p))[0],
                    "where": os.path.dirname(p).replace(HOME, "~"), "mtime": mt,
                    "when": time.strftime("%b %-d, %-I:%M %p", time.localtime(mt))})
    res.sort(key=lambda r: -r["mtime"])
    return res[:40]


WORK = os.path.join(DATA, "Work")


def past_jobs():
    out = []
    dirs = []
    if os.path.isdir(DONE):
        dirs += [(d, os.path.join(DONE, d)) for d in os.listdir(DONE)]
    if os.path.isdir(WORK):
        for c in os.listdir(WORK):
            cd = os.path.join(WORK, c)
            if os.path.isdir(cd):
                dirs += [(f"{m} - {c}", os.path.join(cd, m)) for m in os.listdir(cd) if not m.startswith("_")]
    for d, folder in dirs:
        sp = os.path.join(folder, "summary.json")
        if os.path.exists(sp):
            try:
                s = json.load(open(sp))
            except Exception:
                continue
            out.append({"name": d, "folder": folder, "mtime": os.path.getmtime(sp),
                        "edited": s.get("manual_decisions", False), "cuts": s.get("cuts"),
                        "length": s.get("length"), "fcpxml": s.get("fcpxml")})
    out.sort(key=lambda r: -r["mtime"])
    return out[:20]


def work_folder(name, uid):
    """Same export name + same multicam = same folder (reuse analysis).
    Different multicam under the same name gets its own folder."""
    n = 1
    while True:
        work = os.path.join(DONE, name if n == 1 else f"{name} {n}")
        tagf = os.path.join(work, ".multicam_uid")
        if not os.path.exists(work):
            return work
        if os.path.exists(tagf) and open(tagf).read().strip() == (uid or ""):
            return work
        if not os.path.exists(tagf) and not os.listdir(work):
            return work
        n += 1


def run_job(src, main_ang, audio_ang, uid=None, roles=None, remove_gaps=False):
    name = os.path.splitext(os.path.basename(src.rstrip("/")))[0]
    work = work_folder(name, uid)
    name = os.path.basename(work)
    with LOCK:
        JOB.update(running=True, log=[], progress={}, done=False, ok=None, summary=None,
                   name=name, work=work, started=time.time())
    try:
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, ".multicam_uid"), "w") as fh:
            fh.write(uid or "")
        dest = os.path.join(work, os.path.basename(src.rstrip("/")))
        if os.path.abspath(dest) != os.path.abspath(src):
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dest)
        cmd = [PY, "-u", ENGINE, dest, "--main", main_ang, "--audio", audio_ang, "--out", work,
               "--remove-gaps" if remove_gaps else "--keep-gaps"]
        if roles:
            save_roles(uid, roles)
            cmd += ["--roles", ",".join(f"{k}={v}" for k, v in roles.items() if v)]
        p = subprocess.Popen(awake(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        out_lines = []
        for line in p.stdout:
            line = line.rstrip("\n")
            m = re.match(r"@progress (\w+) (.+) (\d+) (\d+)$", line)
            with LOCK:
                if m:
                    JOB["progress"][f"{m.group(1)}:{m.group(2)}"] = int(m.group(3)) / max(1, int(m.group(4)))
                else:
                    out_lines.append(line)
                    if not line.startswith((" ", "{", "}", "]", "  \"")) and line.strip():
                        JOB["log"].append(line)
        rc = p.wait()
        summ = os.path.join(work, "summary.json")
        ok = rc == 0 and os.path.exists(summ)
        with LOCK:
            JOB["ok"] = ok
            JOB["summary"] = json.load(open(summ)) if ok else None
            if not ok:
                JOB["log"] += out_lines[-15:]
    except Exception as e:
        with LOCK:
            JOB["ok"] = False
            JOB["log"].append(f"Error: {e}")
    finally:
        with LOCK:
            JOB["running"] = False
            JOB["done"] = True


def awake(cmd):
    """Keep the Mac (and its drives) from sleeping while a job runs."""
    if os.path.exists("/usr/bin/caffeinate"):
        return ["/usr/bin/caffeinate", "-ims"] + cmd
    return cmd


INGEST = os.path.join(HERE, "ingest.py")
SYNC = {"running": False, "done": False, "ok": None, "log": [], "progress": {}, "folder": None,
        "result": None, "built": [], "started": None, "stage": None}


def run_sync(folder=None, sync_json=None, labels=None, selection=None, cut=False):
    with LOCK:
        SYNC.update(running=True, done=False, ok=None, log=[], progress={}, started=time.time(),
                    stage="pipeline" if selection is not None else ("build" if sync_json else "scan"),
                    step="", pct=0.0)
        if folder:
            SYNC.update(folder=folder, result=None, built=[], final=None)
        if selection is not None:
            SYNC["final"] = None
    try:
        if selection is not None:
            selp = os.path.join(os.path.dirname(sync_json), "selection.json")
            json.dump(selection, open(selp, "w"), indent=1)
            cmd = [PY, "-u", os.path.join(HERE, "pipeline.py"), sync_json, "--selection", selp]
            if cut:
                cmd.append("--cut")
        elif sync_json:
            cmd = [PY, "-u", INGEST, "build", sync_json, "--dest", INBOX]
            if labels:
                cmd += ["--labels", json.dumps(labels)]
        else:
            cmd = [PY, "-u", INGEST, "scan", folder]
        p = subprocess.Popen(awake(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        tail, built, sj = [], [], None
        for line in p.stdout:
            line = line.rstrip("\n")
            m = re.match(r"@progress (\w+) (\d+) (\d+)$", line)
            with LOCK:
                if line.startswith("@pct "):
                    SYNC["pct"] = float(line.split()[1])
                    continue
                if line.startswith("@stage "):
                    SYNC["step"] = line.split(" ", 3)[3] if len(line.split(" ", 3)) > 3 else ""
                    SYNC["log"].append(SYNC["step"])
                    continue
                if line.startswith("@result "):
                    SYNC["final"] = json.loads(line[len("@result "):])
                    continue
                if m:
                    SYNC["progress"][m.group(1)] = int(m.group(2)) / max(1, int(m.group(3)))
                elif line.startswith("@scan_done "):
                    sj = line[len("@scan_done "):]
                elif line.startswith("@built "):
                    built.append(line[len("@built "):])
                else:
                    tail.append(line)
                    if line.strip():
                        SYNC["log"].append(line)
        rc = p.wait()
        with LOCK:
            SYNC["ok"] = rc == 0
            if rc != 0:
                SYNC["log"] += tail[-12:]
            if sj:
                d = json.load(open(sj))
                groups = {}
                for f in d["files"]:
                    g = groups.setdefault(f["group"], {"name": f["group"], "video": f["video"], "files": 0, "min": 0.0})
                    g["files"] += 1
                    g["min"] += f["duration"] / 60
                moms = d.get("moments", [])
                for mm in moms:
                    for cam, pv in (mm.get("previews") or {}).items():
                        pv["url"] = "/preview?p=" + quote(pv.get("img", ""))
                SYNC["result"] = {"sync_json": sj, "couple": d["couple"], "blocks": d["blocks"], "moments": moms,
                                  "groups": sorted(groups.values(), key=lambda g: (not g["video"], g["name"])),
                                  "weak": [os.path.relpath(w, d["folder"]) for w in d["weak"]],
                                  "silent": [os.path.relpath(w, d["folder"]) for w in d.get("silent", [])],
                                  "files": len(d["files"])}
            if built:
                SYNC["built"] = built
    except Exception as e:
        with LOCK:
            SYNC["ok"] = False
            SYNC["log"].append(f"Error: {e}")
    finally:
        with LOCK:
            SYNC["running"] = False
            SYNC["done"] = True


def sync_progress():
    pr = SYNC["progress"]
    if SYNC["done"]:
        return 1.0
    if SYNC["stage"] == "build":
        return 0.5
    if SYNC["stage"] == "pipeline":
        return round(min(0.99, SYNC.get("pct", 0.0)), 3)
    return round(min(0.99, 0.06 * pr.get("scan", 0) + 0.76 * pr.get("listen", 0) + 0.1 * pr.get("sync", 0)
                     + 0.08 * pr.get("preview", 0)), 3)


def overall_progress():
    pr = JOB["progress"]
    vids = [v for k, v in pr.items() if k.startswith("video:")]
    tr = pr.get("transcript:all", 0.0)
    v = sum(vids) / max(1, len(vids)) if vids else 0.0
    if JOB["done"]:
        return 1.0
    return round(min(0.99, 0.65 * v + 0.33 * tr + (0.02 if vids else 0)), 3)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/":
            b = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/api/exports":
            self._json({"exports": find_exports(), "past": past_jobs()})
        elif self.path == "/api/status":
            with LOCK:
                s = dict(JOB)
                s["progress_total"] = overall_progress()
                s["log"] = JOB["log"][-12:]
                s["elapsed"] = int(time.time() - JOB["started"]) if JOB["started"] else 0
            self._json(s)
        elif self.path == "/api/sync_status":
            with LOCK:
                st = dict(SYNC)
                st["log"] = SYNC["log"][-14:]
                st["progress_total"] = sync_progress()
                st["elapsed"] = int(time.time() - SYNC["started"]) if SYNC["started"] else 0
            self._json(st)
        elif self.path.startswith("/preview?p="):
            fp = os.path.realpath(unquote(self.path[len("/preview?p="):]))
            if not fp.startswith(os.path.realpath(DATA) + os.sep) or not fp.lower().endswith(".jpg") \
                    or not os.path.exists(fp):
                return self.send_error(404)
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/api/ping":
            self._json({"ok": True})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{self.server.server_port}"):
            return self._json({"error": "forbidden"}, 403)
        d = self._body()
        if self.path == "/api/inspect":
            r = subprocess.run([PY, ENGINE, d["path"], "--list-angles"], capture_output=True, text=True)
            try:
                info = json.loads(r.stdout.strip().splitlines()[-1])
                allr = load_roles()
                info["saved_roles"] = allr.get(info.get("uid") or "", None) or allr.get("_last", {})
                info["saved_roles_exact"] = (info.get("uid") or "") in allr
                return self._json(info)
            except Exception:
                return self._json({"error": (r.stderr.strip()[-500:] or "Couldn't read that file.")}, 400)
        if self.path == "/api/run":
            if JOB["running"]:
                return self._json({"error": "Already running"}, 409)
            threading.Thread(target=run_job, args=(d["path"], d["main"], d["audio"], d.get("uid"), d.get("roles"),
                                                   bool(d.get("remove_gaps"))), daemon=True).start()
            return self._json({"ok": True})
        if self.path == "/api/browse":
            r = subprocess.run(["osascript", "-e",
                                'tell application "System Events"\nactivate\n'
                                'set f to choose file with prompt "Pick a Final Cut XML export"\nend tell\n'
                                'POSIX path of f'], capture_output=True, text=True)
            p = r.stdout.strip().rstrip("/")
            return self._json({"path": p} if p else {"path": None})
        if self.path == "/api/pickfolder":
            r = subprocess.run(["osascript", "-e",
                                'tell application "System Events"\nactivate\n'
                                'set f to choose folder with prompt "Pick the wedding folder with all the camera and audio files"\nend tell\n'
                                'POSIX path of f'], capture_output=True, text=True)
            p = r.stdout.strip().rstrip("/")
            return self._json({"path": p or None})
        if self.path == "/api/sync_scan":
            if SYNC["running"] or JOB["running"]:
                return self._json({"error": "Already working on something"}, 409)
            if not os.path.isdir(d.get("folder") or ""):
                return self._json({"error": "That folder isn't reachable. Is the drive plugged in?"}, 400)
            threading.Thread(target=run_sync, kwargs={"folder": d["folder"]}, daemon=True).start()
            return self._json({"ok": True})
        if self.path == "/api/sync_build":
            if SYNC["running"] or not SYNC.get("result"):
                return self._json({"error": "Scan a folder first"}, 409)
            threading.Thread(target=run_sync, kwargs={"sync_json": SYNC["result"]["sync_json"],
                                                      "labels": d.get("labels")}, daemon=True).start()
            return self._json({"ok": True})
        if self.path == "/api/pipeline":
            if SYNC["running"] or JOB["running"] or not SYNC.get("result"):
                return self._json({"error": "Scan a wedding folder first"}, 409)
            sel = [x for x in d.get("selection", []) if x.get("idx")]
            if not sel:
                return self._json({"error": "Check at least one moment"}, 400)
            threading.Thread(target=run_sync, kwargs={"sync_json": SYNC["result"]["sync_json"], "selection": sel,
                                                      "cut": bool(d.get("cut"))}, daemon=True).start()
            return self._json({"ok": True})
        if self.path == "/api/openfcp":
            bad = []
            for f in d.get("files", []):
                if not os.path.exists(f):
                    bad.append(f)
                    continue
                r = subprocess.run(["open", "-a", "Final Cut Pro", f], capture_output=True, text=True)
                if r.returncode != 0:
                    subprocess.run(["open", f])
                time.sleep(1.5)  # let Final Cut take one import at a time
            return self._json({"ok": not bad, "missing": bad})
        if self.path == "/api/open":
            subprocess.run(["open", d.get("folder") or DONE])
            return self._json({"ok": True})
        if self.path == "/api/reveal":
            subprocess.run(["open", "-R", d["file"]])
            return self._json({"ok": True})
        if self.path == "/api/copy":
            subprocess.run(["pbcopy"], input=d["text"], text=True)
            return self._json({"ok": True})
        if self.path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        self.send_error(404)


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Multicam Flow</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--mute:#6e6e73;--line:#e5e3de;--acc:#b8862b;--acc2:#8f6618;--ok:#2f7d4f;--bad:#b3261e}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:760px;margin:0 auto;padding:36px 24px 60px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px}
h1{font-size:26px;margin:0;letter-spacing:-.02em}h1 span{color:var(--acc)}
.sub{color:var(--mute);margin:2px 0 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:16px}
.step{font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--acc2);margin-bottom:10px}
.list{display:flex;flex-direction:column;gap:6px;max-height:300px;overflow:auto}
.item{display:flex;justify-content:space-between;align-items:center;padding:11px 14px;border:1px solid var(--line);border-radius:10px;cursor:pointer;background:#fff}
.item:hover{border-color:var(--acc)}.item.sel{border-color:var(--acc);background:#fbf6ec;box-shadow:inset 0 0 0 1px var(--acc)}
.item b{font-weight:600}.item small{color:var(--mute);display:block}
.when{color:var(--mute);font-size:13px;white-space:nowrap;margin-left:12px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
label{font-size:13px;color:var(--mute);display:block;margin-bottom:6px}
select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font:inherit;background:#fff}
.hint{font-size:13px;color:var(--mute);margin-top:10px}
button{font:inherit;border:0;border-radius:10px;padding:11px 18px;cursor:pointer}
.primary{background:var(--ink);color:#fff;font-weight:600}.primary:disabled{opacity:.35;cursor:default}
.ghost{background:transparent;border:1px solid var(--line);color:var(--ink)}
.ghost:hover{border-color:var(--ink)}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.bar{height:10px;background:#eeece7;border-radius:6px;overflow:hidden;margin:12px 0 8px}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),#d6aa52);transition:width .6s}
.log{font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--mute);background:#faf9f7;border-radius:8px;padding:10px 12px;max-height:150px;overflow:auto;white-space:pre-wrap}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:6px 0 14px}
.stat{background:#faf9f7;border-radius:10px;padding:12px}.stat b{font-size:20px;display:block}.stat span{font-size:12px;color:var(--mute)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.hide{display:none}
.empty{color:var(--mute);padding:14px;border:1px dashed var(--line);border-radius:10px;text-align:center}
.mom{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px;background:#fff}
.mom.off{opacity:.55}
.mhead{display:flex;gap:10px;align-items:center}
.mhead input[type=text]{flex:1;padding:8px 10px;border:1px solid var(--line);border-radius:8px;font:inherit;font-weight:600}
.mhead select{width:160px}
.mhead input[type=checkbox]{width:18px;height:18px;accent-color:var(--acc)}
.cams{display:flex;gap:10px;overflow-x:auto;margin-top:10px;padding-bottom:4px}
.cam{width:150px;flex:none}.cam img{width:150px;height:84px;object-fit:cover;border-radius:8px;background:#eee;display:block}
.cam b{font-size:12px;display:block;margin:4px 0 3px}.cam select{padding:5px 6px;font-size:12px;border-radius:7px}
.check{display:flex;gap:8px;align-items:center;font-size:14px;color:var(--ink);margin:12px 0 0}
.check input{width:18px;height:18px;accent-color:var(--acc)}
.tag{font-size:11px;padding:2px 8px;border-radius:20px;background:#eef6f0;color:var(--ok);margin-left:6px}
</style></head><body><div class="wrap">
<header><div><h1>Multicam <span>Flow</span></h1><p class="sub">Multicam sync and cut for Final Cut Pro</p></div>
<button class="ghost" onclick="quitApp()">Quit</button></header>

<div id="raw" class="card">
 <div class="step">New wedding: start from the raw files</div>
 <p style="margin:0 0 6px">Pick the wedding folder you offloaded. Multicam Flow lines up every camera and mic by sound, finds every stretch where two or more cameras were rolling, and lets you pick which ones to sync and cut.</p>
 <div class="actions" style="margin-top:10px"><button class="primary" onclick="pickFolder()">Choose wedding folder...</button></div>
 <div id="rawprog" class="hide"><div style="margin-top:14px"><b id="rtitle">Scanning</b> <span id="rpct" style="color:var(--mute)"></span></div>
  <div class="bar"><i id="rbar"></i></div>
  <div class="hint" id="rnote">Listening to every file to line them up. The first time on a wedding this takes a while (it reads every file off the drive); it's quick after that.</div>
  <div class="log" id="rlog"></div></div>
 <div id="rawres" class="hide" style="margin-top:14px"></div>
</div>

<div id="pick" class="card">
 <div class="step">1. Pick your export</div>
 <div id="exports" class="list"><div class="empty">Looking for Final Cut exports...</div></div>
 <div class="actions"><button class="ghost" onclick="browse()">Find another file...</button>
 <button class="ghost" onclick="load()">Refresh</button></div>
 <p class="hint">In Final Cut: select your project (ceremony, reception, anything with multicam sections), then File &gt; Export XML. Save it anywhere in Downloads, Desktop or Movies.</p>
</div>

<div id="ask" class="card hide">
 <div class="step">2. A few quick questions</div>
 <p id="mcinfo" style="margin:0 0 14px"></p>
 <div class="row">
  <div><label>Main camera (optional)</label><select id="main"></select>
   <p class="hint" style="margin-top:6px">The camera you'd normally sit on, usually your center gimbal or widest angle. Multicam Flow leans on it a little and cuts away when another angle is better.</p></div>
  <div><label>Clean audio</label><select id="audio"></select>
   <p class="hint" style="margin-top:6px">Your best mic or board feed. It plays under the cut and gets transcribed. Picked the wrong one? Just run it again: the camera analysis is reused, so it's quick.</p></div>
 </div>
 <div style="margin-top:18px"><label style="font-weight:600;color:var(--ink)">Who does each camera mainly show?</label>
  <p class="hint" style="margin-top:2px">This is how Multicam Flow stays on whoever is talking and catches the couple's reactions. Remembered for next time.</p>
  <div id="roles" class="row" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))"></div>
  <p class="hint" id="lavs"></p></div>
 <label style="display:flex;gap:10px;align-items:flex-start;margin-top:18px;color:var(--ink);font-size:14px;cursor:pointer">
  <input type="checkbox" id="gaps" style="margin-top:3px;width:16px;height:16px">
  <span><b>Cut out gaps where no camera has video</b><br><span class="hint" style="margin:0">Any stretch of 1 second or more with no camera rolling (or all black) gets removed and the timeline closes up. The audio under it goes too.</span></span></label>
 <div class="actions"><button id="go" class="primary" onclick="run()">Analyze and cut</button></div>
</div>

<div id="prog" class="card hide">
 <div class="step">3. Working</div>
 <div><b id="ptitle">Analyzing</b> <span id="pct" style="color:var(--mute)"></span></div>
 <div class="bar"><i id="barfill"></i></div>
 <div class="hint" id="pnote">Scoring every camera, then transcribing the audio. You can leave this window open and do other things.</div>
 <div class="log" id="log"></div>
</div>

<div id="result" class="card hide">
 <div class="step">4. Your draft cut is ready</div>
 <div class="stats"><div class="stat"><b id="sCuts">-</b><span>cuts</span></div>
  <div class="stat"><b id="sLen">-</b><span>length</span></div>
  <div class="stat"><b id="sMom">-</b><span>moments flagged</span></div></div>
 <div id="sTime" class="hint" style="margin-top:0"></div>
 <div class="actions"><button class="primary" onclick="openFcp([last.summary.fcpxml])">Open in Final Cut</button>
 <button class="ghost" onclick="reveal()">Show the XML in Finder</button>
 <button class="ghost" onclick="openFolder()">Open project folder</button>
 <button class="ghost" onclick="reset()">Cut another</button></div>
 <p class="hint" id="importhint">Import it in Final Cut: File &gt; Import &gt; XML.</p>
 
</div>

<div id="pastc" class="card hide"><div class="step">Recent cuts</div><div id="past" class="list"></div></div>
</div>
<script>
let sel=null, info=null, last=null, poll=null;
const $=id=>document.getElementById(id);
async function api(p,b){const r=await fetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{});return r.json()}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){
 const d=await api('/api/exports');
 const el=$('exports');
 if(!d.exports.length){el.innerHTML='<div class="empty">No Final Cut exports found yet. Export one from Final Cut (File &gt; Export XML), then click Refresh.</div>'}
 else el.innerHTML=d.exports.map((e,i)=>`<div class="item" data-i="${i}"><div><b>${esc(e.name)}</b><small>${esc(e.where)}</small></div><div class="when">${esc(e.when)}</div></div>`).join('');
 el.querySelectorAll('.item').forEach(n=>n.onclick=()=>choose(d.exports[n.dataset.i].path,n));
 if(d.past.length){$('pastc').classList.remove('hide');
  $('past').innerHTML=d.past.map((p,i)=>`<div class="item" data-i="${i}"><div><b>${esc(p.name)}</b>${p.edited?'<span class="tag">edited</span>':''}<small>${p.cuts} cuts, ${esc(p.length)}</small></div><div class="when">Open</div></div>`).join('');
  $('past').querySelectorAll('.item').forEach(n=>n.onclick=()=>api('/api/open',{folder:d.past[n.dataset.i].folder}));}
}
async function choose(path,node){
 document.querySelectorAll('#exports .item').forEach(x=>x.classList.remove('sel')); if(node)node.classList.add('sel');
 sel=path; $('ask').classList.remove('hide'); $('mcinfo').textContent='Reading...'; $('go').disabled=true;
 info=await api('/api/inspect',{path});
 if(info.error){$('mcinfo').innerHTML='<span class="bad">'+esc(info.error)+'</span>';return}
 const vid=info.angles.filter(a=>a.video).map(a=>a.name), all=info.angles.map(a=>a.name);
 $('main').innerHTML='<option value="none" selected>None, every angle competes equally</option>'+vid.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
 const keep = info.from_timeline ? `<option value="timeline" selected>Keep my timeline's audio (${esc(info.timeline_audio.join(', '))})</option>` : '';
 $('audio').innerHTML=keep+all.map(n=>`<option value="${esc(n)}" ${(!info.from_timeline&&n==info.audio)?'selected':''}>${esc(n)}</option>`).join('');
 if(info.mode==='inplace'){
  $('mcinfo').innerHTML=`<b>${esc(info.projects.join(', '))}</b>: recutting <b>${info.section_count} multicam section${info.section_count>1?'s':''}</b> (${esc(info.total_text.replace(/\.\d+$/,''))} total) in place.<br><span class="hint">Titles, graphics, adjustment layers, transitions and music stay exactly where they are. You get a copy of the project named "${esc(info.projects[0])} Multicam Flow".</span>`;
 } else {
  $('mcinfo').innerHTML=`<b>${esc(info.multicam)}</b>, cutting ${esc(info.range_text)} into a new project`;
 }
 const ROLE_OPTS=[['', 'Not sure'],['bride',"Bride's face"],['groom',"Groom's face"],['officiant','Officiant'],['couple','Couple (wide)'],['gimbal','Gimbal / roaming'],['guests','Guests'],['other','Other']];
 const saved=info.saved_roles||{};
 $('roles').innerHTML=vid.map(n=>`<div><label>${esc(n)}</label><select data-cam="${esc(n)}">${ROLE_OPTS.map(([v,l])=>`<option value="${v}" ${saved[n]===v?'selected':''}>${l}</option>`).join('')}</select></div>`).join('');
 $('lavs').textContent = info.lav_roles && info.lav_roles.length ? ('Lav mics found: '+info.lav_roles.join(', ')+'. Multicam Flow will follow whoever is talking.') : 'No lav folders named Groom / Bride / Officiant found, so talking is detected from the transcript only.';
 $('go').disabled=false; $('ask').scrollIntoView({behavior:'smooth'});
}
async function browse(){const r=await api('/api/browse',{}); if(r.path)choose(r.path,null)}
async function run(){
 $('go').disabled=true;
 const roles={}; document.querySelectorAll('#roles select').forEach(s=>{roles[s.dataset.cam]=s.value});
 const r=await api('/api/run',{path:sel,main:$('main').value,audio:$('audio').value,uid:info.uid,roles,remove_gaps:$('gaps').checked});
 if(r.error){alert(r.error);$('go').disabled=false;return}
 $('pick').classList.add('hide');$('ask').classList.add('hide');$('result').classList.add('hide');$('prog').classList.remove('hide');
 poll=setInterval(status,1500);
}
function fmt(s){const m=Math.floor(s/60);return m+':'+String(s%60).padStart(2,'0')}
async function status(){
 const s=await api('/api/status'); last=s;
 $('barfill').style.width=Math.round(s.progress_total*100)+'%';
 $('pct').textContent=Math.round(s.progress_total*100)+'%  ·  '+fmt(s.elapsed);
 $('ptitle').textContent='Analyzing '+(s.name||'');
 $('log').textContent=s.log.join('\n'); $('log').scrollTop=1e9;
 if(s.done){clearInterval(poll);
  if(!s.ok){$('ptitle').innerHTML='<span class="bad">Something went wrong</span>';$('pnote').textContent='The details are below. See Troubleshooting in the README.';
   $('prog').insertAdjacentHTML('beforeend','<div class="actions"><button class="ghost" onclick="reset()">Back</button></div>');return}
  const m=s.summary; $('prog').classList.add('hide'); $('result').classList.remove('hide');
  $('sCuts').textContent=m.cuts; $('sLen').textContent=m.length.replace(/\.\d+$/,'');
  $('sMom').textContent=(m.loudness_moments||[]).length+(m.named_moments||[]).length;
  $('sTime').textContent='Screen time: '+Object.entries(m.screen_time).map(([k,v])=>k+' '+v.replace(/\.\d+$/,'')).join(', ')+(m.transcript_lines?' · transcript: '+m.transcript_lines+' lines':' · no transcript');
  if(m.inplace_stats && m.inplace_stats.gaps_removed){$('sTime').textContent+=' · cut out '+m.inplace_stats.gaps_removed+' no-video gap(s), '+m.inplace_stats.gap_seconds.toFixed(1)+'s'}
  if(m.mode==='recut in place'){$('importhint').textContent='Import it in Final Cut: File > Import > XML. It shows up as "'+m.projects[0]+' Multicam Flow" next to your original, with all your titles and graphics in place.'}
  load();}
}
let rpoll=null, rres=null;
const ROLES=[['','Not sure'],['gimbal','Roaming / gimbal'],['wide','Wide'],['bride',"Bride's face"],['groom',"Groom's face"],['couple','Couple'],['officiant','Officiant'],['guests','Guests'],['other','Speaker / other']];
const tm=t=>new Date(t*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
async function pickFolder(){
 const r=await api('/api/pickfolder',{}); if(!r.path)return;
 const s=await api('/api/sync_scan',{folder:r.path}); if(s.error){alert(s.error);return}
 $('rawres').classList.add('hide'); $('rawprog').classList.remove('hide'); $('rtitle').textContent='Scanning '+r.path.split('/').pop();
 $('rnote').textContent="Listening to every file to line them up. The first time on a wedding this takes a while (it reads every file off the drive); it's quick after that.";
 rpoll=setInterval(syncStatus,1500);
}
async function syncStatus(){
 const s=await api('/api/sync_status');
 $('rbar').style.width=Math.round(s.progress_total*100)+'%';
 $('rpct').textContent=Math.round(s.progress_total*100)+'%  ·  '+fmt(s.elapsed);
 if(s.stage==='pipeline'&&s.step)$('rtitle').textContent=s.step;
 $('rlog').textContent=s.log.join('\n'); $('rlog').scrollTop=1e9;
 if(!s.done)return;
 clearInterval(rpoll);
 if(!s.ok){$('rtitle').innerHTML='<span class="bad">Something went wrong</span>';$('rnote').textContent='The details are below. See Troubleshooting in the README.';return}
 $('rawprog').classList.add('hide');
 if(s.stage==='pipeline'&&s.final){showFinal(s.final);return}
 if(s.result){rres=s.result; showMoments();}
}
function showMoments(){
 const r=rres, el=$('rawres'); el.classList.remove('hide');
 const ms=r.moments||[];
 const cams=r.groups.map(g=>`${esc(g.name)} <span class="hint">(${g.files} ${g.video?'clips':'audio'}, ${Math.round(g.min)} min)</span>`).join(' · ');
 const rows=ms.map((m,i)=>{
  const info=[tm(m.start)+' to '+tm(m.end), Math.round((m.end-m.start)/60)+' min', (m.audio||[]).length?'audio: '+m.audio.join(', '):'', m.note].filter(Boolean).join(' · ');
  const camh=m.cams.map(c=>{const pv=(m.previews||{})[c]||{};
   return `<div class="cam">${pv.url?`<img src="${pv.url}" loading="lazy">`:'<img>'}<b>${esc(c)}${pv.guess?` <span class="hint" style="font-weight:400">· ${esc(pv.guess)}</span>`:''}</b>
   <select data-m="${i}" data-c="${esc(c)}">${ROLES.map(([v,l])=>`<option value="${v}" ${v===(pv.role||'')?'selected':''}>${l}</option>`).join('')}</select></div>`}).join('');
  return `<div class="mom ${m.checked?'':'off'}" id="mom${i}"><div class="mhead"><input type="checkbox" data-i="${i}" ${m.checked?'checked':''} onchange="$('mom${i}').classList.toggle('off',!this.checked)">
   <input type="text" id="mn${i}" value="${esc(m.name)}"><select id="ms${i}"><option value="ceremony" ${m.style==='ceremony'?'selected':''}>Ceremony style</option><option value="party" ${m.style==='party'?'selected':''}>Party style</option></select></div>
   <div class="hint" style="margin-top:6px">${esc(info)}</div><div class="cams">${camh}</div></div>`}).join('');
 const sil=(r.silent||[]).length?` ${r.silent.length} clip(s) have no sound at all (drone?) and use their recording time.`:'';
 const weak=r.weak.length?`<p class="hint">${r.weak.length} b-roll clip(s) had nothing to match against, so they're placed by the camera's clock (usually within a second).${sil}</p>`:`<p class="hint ok">Every clip with sound matched.${sil}</p>`;
 el.innerHTML=`<p style="margin:0 0 8px"><b>${esc(r.couple)}</b>: ${r.files} files synced.</p><p class="hint" style="margin-top:0">${cams}</p>
  <label style="font-weight:600;color:var(--ink);margin-top:14px">Moments where two or more cameras rolled together. Check the ones to sync and name them anything:</label>
  ${rows||'<div class="empty">No stretches with two or more cameras rolling together.</div>'}
  <p class="hint">Same name = one multicam (a reception with a dinner break stays one reception). <b>Ceremony style</b> holds on whoever's talking and cuts to reactions. <b>Party style</b> cuts faster and favors the moving camera. Camera guesses are optional: fix only the wrong ones.</p>
  ${weak}
  <label class="check"><input type="checkbox" id="autocut" checked onchange="$('gob').textContent=this.checked?'Sync and cut':'Sync only'"> Cut automatically after syncing</label>
  <p class="hint" style="margin-top:4px">Finished files go to Movies/Multicam Flow/${esc(r.couple)}.</p>
  <div class="actions"><button class="primary" id="gob" onclick="runPipeline()">Sync and cut</button></div>`;
}
async function runPipeline(){
 const ms=rres.moments||[]; const sel=[];
 ms.forEach((m,i)=>{const cb=document.querySelector(`.mom input[data-i="${i}"]`); if(!cb||!cb.checked)return;
  const roles={}; document.querySelectorAll(`select[data-m="${i}"]`).forEach(x=>{if(x.value)roles[x.dataset.c]=x.value});
  sel.push({idx:[i],name:$('mn'+i).value.trim()||m.name,style:$('ms'+i).value,roles});});
 if(!sel.length){alert('Check at least one moment.');return}
 const cut=$('autocut').checked;
 const r=await api('/api/pipeline',{selection:sel,cut}); if(r.error){alert(r.error);return}
 $('rawres').classList.add('hide'); $('rawprog').classList.remove('hide');
 $('rtitle').textContent=cut?'Syncing and cutting':'Syncing';
 $('rnote').textContent=cut?'Each moment gets analyzed frame by frame, then cut. A 30-minute ceremony takes about 15 minutes. You can walk away; your Mac stays awake until it finishes.':'';
 rpoll=setInterval(syncStatus,1500);
}
let fin=null;
function showFinal(f){
 fin=f; const el=$('rawres'); el.classList.remove('hide');
 const files=f.moments.map(m=>m.cut||m.synced).filter(Boolean);
 const rows=f.moments.map((m,i)=>{const fp=m.cut||m.synced; const st=m.cut?`${m.cuts} cuts · ${m.length||''}${m.gaps?` · ${m.gaps} min of empty gaps removed`:''}`:(f.cut?'<span class="bad">cut failed, synced version saved</span>':'synced multicam + project');
  return `<div class="item" style="cursor:default"><div><b>${esc(m.name)}</b> <span class="hint">${m.style==='party'?'Party':'Ceremony'} style</span><small>${st}</small></div>
  <div style="display:flex;gap:8px"><button class="ghost" onclick="openFcp([fin.moments[${i}].cut||fin.moments[${i}].synced])">Open in Final Cut</button><button class="ghost" onclick="api('/api/reveal',{file:'${esc(fp).replace(/'/g,"\\'")}'})">Show</button></div></div>`}).join('');
 const warn=(f.warnings||[]).length?`<p class="hint bad">${f.warnings.map(esc).join('<br>')}</p>`:'';
 el.innerHTML=`<p class="ok" style="margin:0 0 8px"><b>Done: ${esc(f.couple)}</b></p>
  <div class="list" style="max-height:none">${rows}</div>${warn}
  <div class="actions"><button class="primary" onclick="openFcp(${esc(JSON.stringify(files))})">Open all in Final Cut</button>
  <button class="ghost" onclick="api('/api/open',{folder:fin.out_dir})">Show folder</button></div>
  <p class="hint">Saved in Movies/Multicam Flow/${esc(f.couple)}${f.cut?' (uncut versions in "Synced only")':''}. You can also drag the files from Finder onto Final Cut.</p>`;
 load();
}
async function openFcp(files){const r=await api('/api/openfcp',{files}); if(r.missing&&r.missing.length)alert('Missing: '+r.missing.join(', '))}
function reveal(){api('/api/reveal',{file:last.summary.fcpxml})}
function openFolder(){api('/api/open',{folder:last.work})}
function reset(){location.reload()}
async function quitApp(){await api('/api/quit',{});document.body.innerHTML='<div class="wrap"><h1>Multicam <span>Flow</span></h1><p class="sub">Closed. Double-click Multicam Flow to open it again.</p></div>'}
load();
fetch('/api/sync_status').then(r=>r.json()).then(s=>{if(s.running){$('rawprog').classList.remove('hide');rpoll=setInterval(syncStatus,1500)} else if(s.done&&s.ok){if(s.stage==='pipeline'&&s.final)showFinal(s.final);else if(s.result){rres=s.result;showMoments()}}});
fetch('/api/status').then(r=>r.json()).then(s=>{if(s.running){$('pick').classList.add('hide');$('prog').classList.remove('hide');poll=setInterval(status,1500)}});
</script></body></html>
"""


def main():
    # Already open? Just bring the window back.
    if os.path.exists(PORT_FILE):
        try:
            port = int(open(PORT_FILE).read().strip())
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                webbrowser.open(f"http://127.0.0.1:{port}/")
                return
        except Exception:
            pass
    os.makedirs(DONE, exist_ok=True)
    os.makedirs(INBOX, exist_ok=True)
    # Check Apple Vision in the background and leave a note for troubleshooting.
    threading.Thread(target=lambda: subprocess.run([PY, ENGINE, "--vision-check"], capture_output=True),
                     daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_port
    open(PORT_FILE, "w").write(str(port))
    url = f"http://127.0.0.1:{port}/"
    print(f"Multicam Flow is open at {url}")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    finally:
        try:
            os.remove(PORT_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
