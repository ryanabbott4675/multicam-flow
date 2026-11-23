#!/usr/bin/env python3
"""Multicam Flow Ingest: point it at a wedding folder of raw camera and audio files.

1. Scans every video and audio file (length, recording time, frame rate).
2. Groups files by camera / audio role using folder names
   (Video/Camera 1, Audio/Groom ...), falling back to camera model or file prefix.
3. Syncs everything by matching audio (onset fingerprints), the same idea as
   Final Cut's "sync by audio", using recording times only as a starting guess.
4. Splits the day into blocks (ceremony, reception ...) by gaps in coverage.
5. Writes one Final Cut XML per section with a synced multicam and a project,
   ready for Multicam Flow.

Usage:
  python3 ingest.py scan  <folder> [--out DIR]        -> DIR/sync.json (+ report)
  python3 ingest.py build <DIR/sync.json> [--labels '{"1":"Ceremony","2":"Reception"}'] [--dest DIR]
  python3 ingest.py all   <folder>                     scan + build with default labels
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fractions import Fraction
from urllib.parse import quote

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
# Working files (caches, logs, results). The Mac app points this at ~/Library/Application Support.
DATA = os.environ.get("MCF_DATA") or HERE
os.makedirs(DATA, exist_ok=True)
VIDEO_EXT = {".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".m4v", ".insv", ".braw", ".crm"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aif", ".aiff", ".flac", ".bwf"}
SKIP_DIRS = ("Render Files", "Analysis Files", "Shared Items", "Transcoded Media", "Proxy Media",
             "High Quality Media", "Thumbnails", "Peaks Data", "Done", "Sync Cache", "Inbox")
# finished films, music beds, photos: never camera footage
SKIP_NAMES = re.compile(r"project files|export|deliver|final|music|song|graphic|logo|photo|stills|"
                        r"\blut|sfx|sound effects|template|instagram|social|teaser|highlight", re.I)
SKIP_DIR_SUFFIX = (".fcpbundle", ".fcpcache", ".app", ".photoslibrary", ".fcpxmld", ".lrdata", ".cocatalog")
# Card folder names that never mean "which camera"
GENERIC = re.compile(r"^(private|m4root|clip|clips|sub|xdroot|contents|video|videos|footage|dcim|"
                     r"\d{3}[a-z_]*|\d{3}canon|\d{3}gopro|\d{3}media|avchd|bdmv|stream|media|raw|"
                     r"card ?\d*|a ?\d+|day ?\d*|audio|sound|\d+)$", re.I)
ROLE_NAMES = ["groom", "bride", "officiant", "lapel", "lavalier", "wireless", "pastor", "priest", "rabbi", "dj", "board", "band",
              "podium", "mic", "speech", "toast", "lav", "zoom", "tascam", "recorder"]
SR = 8000          # analysis sample rate
HOP = 80           # 10 ms frames -> 100 feature samples / sec
FPS_FEAT = SR // HOP
COARSE = 10        # coarse timeline rate (samples / sec)
LOG_FH = None


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if LOG_FH:
        LOG_FH.write(msg + "\n")
        LOG_FH.flush()


def progress(stage, k, n):
    print(f"@progress {stage} {k} {n}", file=sys.stderr, flush=True)


def find_ffmpeg():
    for c in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.exists(c):
            return c
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    sys.exit("ffmpeg not found")


# ---------------------------------------------------------------- scanning
def walk_media(root):
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if not d.startswith(".") and d not in SKIP_DIRS
                       and not d.endswith(SKIP_DIR_SUFFIX) and "proxy" not in d.lower()
                       and not SKIP_NAMES.search(d))
        for f in sorted(fn):
            if f.startswith("._") or f.startswith("."):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXT or ext in AUDIO_EXT:
                out.append(os.path.join(dp, f))
    return out


FPS_MAP = {"23.98": Fraction(24000, 1001), "23.976": Fraction(24000, 1001), "29.97": Fraction(30000, 1001),
           "59.94": Fraction(60000, 1001), "47.95": Fraction(48000, 1001), "119.88": Fraction(120000, 1001),
           "239.76": Fraction(240000, 1001)}


def parse_fps(s):
    if s in FPS_MAP:
        return FPS_MAP[s]
    try:
        v = float(s)
    except ValueError:
        return None
    if abs(v - round(v)) < 0.01:
        return Fraction(int(round(v)))
    return Fraction(v).limit_denominator(1001)


def parse_time(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            d = datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            t = d.timestamp()
            return t if t > 946684800 else None   # ignore unset clocks (before 2000)
        except ValueError:
            continue
    return None


def probe_cached(ffmpeg, path, cache_dir):
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.basename(path)}|{st.st_size}|{int(st.st_mtime)}|p1".encode()).hexdigest()[:16]
    cp = os.path.join(cache_dir, "probe_" + key + ".json")
    if os.path.exists(cp):
        try:
            info = json.load(open(cp))
            info["path"] = path
            return info
        except Exception:
            pass
    info = probe(ffmpeg, path)
    try:
        json.dump(info, open(cp, "w"))
    except OSError:
        pass
    return info


def probe(ffmpeg, path):
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path], capture_output=True, text=True,
                       errors="replace")
    txt = r.stderr
    info = {"path": path, "size": os.path.getsize(path), "mtime": os.path.getmtime(path),
            "video": False, "audio": False}
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    info["duration"] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    meta = {}
    for line in txt.splitlines():
        mm = re.match(r"\s{4,}([A-Za-z0-9_.\-]+)\s*:\s?(.*)$", line)
        if mm and mm.group(1).lower() not in meta:
            meta[mm.group(1).lower()] = mm.group(2).strip()
    vm = re.search(r"Stream #\S+.*?: Video: (\w+).*?, (\d{2,5})x(\d{2,5})[ ,\[].*?([\d.]+) (?:fps|tbr)", txt)
    if vm and "attached pic" not in txt[vm.start():vm.start() + 400].split("\n")[0]:
        info["video"] = True
        info["codec"] = vm.group(1)
        info["w"], info["h"] = int(vm.group(2)), int(vm.group(3))
        info["fps"] = str(parse_fps(vm.group(4)) or Fraction(24000, 1001))
    am = re.search(r"Stream #\S+.*?: Audio: (\w+).*?, (\d+) Hz, ([^,]+)", txt)
    if am:
        info["audio"] = True
        info["rate"] = int(am.group(2))
        ch = am.group(3).strip()
        info["channels"] = {"mono": 1, "stereo": 2}.get(ch, int(re.match(r"\d+", ch).group()) if re.match(r"\d+", ch) else 2)
        info["audio_streams"] = len(re.findall(r"Stream #\S+.*?: Audio:", txt))
    info["timecode"] = meta.get("timecode")
    info["time_reference"] = meta.get("time_reference")
    info["model"] = meta.get("com.apple.quicktime.model") or meta.get("model") or meta.get("make") or ""
    ct = parse_time(meta.get("creation_time"))
    if ct is None and meta.get("date"):
        ct = parse_time(meta.get("date") + " " + meta.get("time", "00:00:00").replace("-", ":"))
    info["ctime"] = ct
    return info


def group_for(path, root, is_video, model):
    rel = os.path.relpath(os.path.dirname(path), root)
    parts = [] if rel == "." else rel.split(os.sep)
    named = [p for p in parts if not GENERIC.match(p.strip())]
    if not is_video:
        for p in reversed(parts):
            if any(w in p.lower() for w in ROLE_NAMES):
                return p
        if named:
            return named[-1]
        return os.path.splitext(os.path.basename(path))[0].rstrip("0123456789_- ") or "Audio"
    for p in reversed(named):
        if re.search(r"cam|camera|gimbal|drone|wide|tight|angle|ronin|side|center|main|a7|fx\d|r\d|gh\d|bmpcc|pocket|iphone",
                     p, re.I):
            return p
    if named:
        return named[-1]
    return None   # decide later by model / prefix


def name_prefix(path):
    b = os.path.splitext(os.path.basename(path))[0]
    m = re.match(r"([A-Za-z_\-]*\d?)", b)
    return (m.group(1) if m else b[:4]) or b[:4]


# ---------------------------------------------------------------- audio features
def cache_key(info):
    h = hashlib.sha1(f"{os.path.basename(info['path'])}|{info['size']}|{int(info['mtime'])}|v2".encode()).hexdigest()[:16]
    return h


PART = 300.0        # long files are processed in 5-minute pieces (resumable)


def raw_flux(ffmpeg, path, ss=None, t=None, stream=None):
    cmd = [ffmpeg, "-nostdin", "-v", "error"]
    if ss is not None:
        cmd += ["-ss", f"{ss:.3f}"]
    cmd += ["-i", path]
    if t is not None:
        cmd += ["-t", f"{t:.3f}"]
    cmd += ["-vn", "-sn", "-dn"]
    if stream is not None:
        cmd += ["-map", f"0:a:{stream}"]
    cmd += ["-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    win = 512
    nb = 20
    freqs = np.fft.rfftfreq(win, 1.0 / SR)
    edges = np.geomspace(120, 3800, nb + 1)
    band = np.digitize(freqs, edges) - 1
    W = np.hanning(win).astype(np.float32)
    feats = []
    carry = np.zeros(0, np.float32)
    prev = None
    chunk = SR * 60 * 2
    while True:
        raw = p.stdout.read(chunk)
        if not raw:
            break
        x = np.frombuffer(raw[: len(raw) // 2 * 2], np.int16).astype(np.float32) / 32768.0
        x = np.concatenate([carry, x])
        n = (len(x) - win) // HOP + 1
        if n <= 0:
            carry = x
            continue
        idx = np.arange(win)[None, :] + HOP * np.arange(n)[:, None]
        S = np.abs(np.fft.rfft(x[idx] * W, axis=1)) ** 2
        E = np.zeros((n, nb), np.float32)
        for b_ in range(nb):
            sel = band == b_
            if sel.any():
                E[:, b_] = S[:, sel].sum(1)
        L = np.log(E + 1e-9)
        if prev is None:
            prev = L[:1]
        d = np.diff(np.vstack([prev, L]), axis=0)
        prev = L[-1:]
        feats.append(np.maximum(d, 0).sum(1).astype(np.float32))
        carry = x[n * HOP:]
    p.wait()
    f = np.concatenate(feats) if feats else np.zeros(0, np.float32)
    if t is not None:   # exact length so pieces line up end to end
        want = int(round(t * FPS_FEAT))
        f = np.pad(f, (0, max(0, want - len(f))))[:want]
    return f


def n_parts(duration):
    return int(np.ceil(duration / PART)) if duration > PART * 3 else 1


def part_path(cache_dir, key, k):
    return os.path.join(cache_dir, f"{key}_p{k}.npy")


def do_part(ffmpeg, info, cache_dir, k, key=None):
    key = key or cache_key(info)
    pp = part_path(cache_dir, key, k)
    if not os.path.exists(pp):
        f = raw_flux(ffmpeg, info["path"], k * PART, PART)
        np.save(pp + ".tmp.npy", f)
        os.replace(pp + ".tmp.npy", pp)


def onset_feature(ffmpeg, path, cache_dir, key, info=None):
    cp = os.path.join(cache_dir, key + ".npy")
    if os.path.exists(cp):
        try:
            return np.load(cp)
        except Exception:
            pass
    n = n_parts(info["duration"]) if info else 1
    if n == 1:
        f = raw_flux(ffmpeg, path)
    else:
        for k in range(n):
            do_part(ffmpeg, info, cache_dir, k, key)
        f = np.concatenate([np.load(part_path(cache_dir, key, k)) for k in range(n)])
        f = f[: int(info["duration"] * FPS_FEAT)]
    f = normalize(f)
    try:
        np.save(cp, f)
        for k in range(n if n > 1 else 0):
            os.remove(part_path(cache_dir, key, k))
    except OSError:
        pass
    return f


def normalize(f, w=FPS_FEAT * 3):
    """Remove slow loudness changes so different mics look alike: local z-score, clipped."""
    if len(f) < 10:
        return f.astype(np.float32)
    k = np.ones(w, np.float32) / w
    pad = np.pad(f, (w // 2, w - w // 2 - 1), mode="edge")
    mu = np.convolve(pad, k, "valid")
    sd = np.sqrt(np.maximum(np.convolve(pad ** 2, k, "valid") - mu ** 2, 1e-6))
    z = (f - mu) / (sd + 0.05 * np.median(sd) + 1e-6)
    return np.clip(z, -2, 6).astype(np.float32)


def xcorr(a, b):
    """c[k] = sum a[n+k]*b[n] for k in -(len(b)-1)..len(a)-1; returns (c, lag0_index)."""
    n = len(a) + len(b) - 1
    N = 1 << int(np.ceil(np.log2(max(n, 2))))
    A = np.fft.rfft(a, N)
    B = np.fft.rfft(b, N)
    c = np.fft.irfft(A * np.conj(B), N).astype(np.float64)
    c = np.concatenate([c[-(len(b) - 1):], c[:len(a)]]) if len(b) > 1 else c[:len(a)]
    return c, len(b) - 1


def peak_conf(c, center_idx, lo, hi, rate, excl_s=0.5):
    """Best peak in c[lo:hi], plus a confidence score (peak height vs the rest)."""
    lo, hi = max(lo, 0), min(hi, len(c))
    if hi - lo < 5:
        return None, 0.0
    seg = c[lo:hi]
    i = int(np.argmax(seg))
    pk = seg[i]
    ex = int(excl_s * rate)
    mask = np.ones(len(seg), bool)
    mask[max(0, i - ex): i + ex + 1] = False
    rest = seg[mask]
    if len(rest) < 5:
        return None, 0.0
    med = np.median(rest)
    mad = np.median(np.abs(rest - med)) * 1.4826 + 1e-9
    second = rest.max()
    z = (pk - med) / mad
    ratio = (pk - med) / max(second - med, 1e-9)
    # sub-sample refinement
    j = lo + i
    off = 0.0
    if 0 < j < len(c) - 1:
        y0, y1, y2 = c[j - 1], c[j], c[j + 1]
        den = y0 - 2 * y1 + y2
        if abs(den) > 1e-12:
            off = 0.5 * (y0 - y2) / den
    lag = float((j + off - center_idx) / rate)
    conf = float(min(z / 8.0, 3.0) * min(ratio, 3.0))
    return lag, conf


# ---------------------------------------------------------------- sync
def coarse_track(items, feats, t0, span, rate=COARSE, mode="start"):
    n = int(span * rate) + 1
    tr = np.zeros(n, np.float32)
    k = FPS_FEAT // rate
    for it in items:
        f = feats.get(it["id"])
        if f is None or len(f) < k:
            continue
        g = f[: len(f) // k * k].reshape(-1, k).max(1)
        st = it["guess"][mode] - t0
        i0 = int(round(st * rate))
        if i0 >= n or i0 + len(g) <= 0:
            continue
        a, b = max(i0, 0), min(i0 + len(g), n)
        tr[a:b] = np.maximum(tr[a:b], g[a - i0: b - i0])
    return tr


def sync(items, feats, log=log):
    global feats_g
    feats_g = feats
    """items: list of dicts with id, group, duration, guess{start,end}. Returns {id: absolute start}."""
    # Cameras keep one reliable clock per camera. Audio recorders don't (chunked files share a
    # stamp, some jump by an hour), so every audio file is placed on its own by sound.
    for it in items:
        it["unit"] = it["group"] if it["video"] else f"{it['group']}|{twin_stem(it)}"
    groups = {}
    for it in items:
        groups.setdefault(it["unit"], []).append(it)
    # ---- step 1: clock offset per group (camera clocks disagree, sometimes by hours).
    # Every clip pair across two groups votes for an offset; the right answer gets many votes.
    tot = {g: sum(i["duration"] for i in v) for g, v in groups.items()}
    vid_units = [g for g in groups if groups[g][0]["video"]]
    ref = max(vid_units or groups, key=lambda g: (tot[g], -len(groups[g])))
    log(f"  reference: {ref}")
    k = FPS_FEAT // COARSE
    low = {}
    for it in items:
        f = feats[it["id"]]
        low[it["id"]] = f[: len(f) // k * k].reshape(-1, k).max(1) if len(f) >= k else f
    pick = {g: sorted(v, key=lambda i: -i["duration"])[:40] for g, v in groups.items()}
    gl = sorted(groups)
    pairw = {}   # (g,h) -> {(mg,mh): (offset, weight)}
    for x, g in enumerate(gl):
        for h in gl[x + 1:]:
            votes = []
            for A in pick[g]:
                la = low[A["id"]]
                for B in pick[h]:
                    lb = low[B["id"]]
                    if min(len(la), len(lb)) < COARSE * 8:
                        continue
                    c, z0 = xcorr(la - la.mean(), lb - lb.mean())
                    lag, cf = peak_conf(c, z0, 0, len(c), COARSE, excl_s=3.0)
                    if lag is None or cf < 0.8:
                        continue
                    # need real overlap at that lag
                    ov = min(A["duration"], lag + B["duration"]) - max(0, lag)
                    if ov < 8:
                        continue
                    votes.append((A, B, lag, cf * min(ov / 60.0, 3.0)))
            res = {}
            for mg in ("start", "end"):
                for mh in ("start", "end"):
                    # offset to add to h's clock so that it lines up with g's clock
                    vs = [(A["guess"][mg] + lag - B["guess"][mh], w) for A, B, lag, w in votes]
                    if not vs:
                        continue
                    best = None
                    for o, _ in vs:
                        wsum = sum(w for o2, w in vs if abs(o2 - o) <= 2.5)
                        if best is None or wsum > best[1]:
                            best = (o, wsum)
                    inl = [o2 for o2, w in vs if abs(o2 - best[0]) <= 2.5]
                    res[(mg, mh)] = (float(np.median(inl)), best[1], len(inl))
            pairw[(g, h)] = res
    # choose start/end stamp reading per group: the assignment that makes the most votes agree
    import itertools
    best_assign, best_score = None, -1
    gl_m = sorted([g for g in gl if len(groups[g]) > 1], key=lambda g: -tot[g])   # one-file units: offset absorbs it
    for combo in itertools.product(("start", "end"), repeat=min(len(gl_m), 10)):
        md = {g: "start" for g in gl}
        md.update(dict(zip(gl_m, combo)))
        sc = sum(r.get((md[g], md[h]), (0, 0, 0))[1] for (g, h), r in pairw.items()
                 if r.get((md[g], md[h]), (0, 0, 0))[2] >= 2)
        if sc > best_score + 1e-9:
            best_assign, best_score = md, sc
    md = best_assign
    # spanning tree from the reference over the strongest links
    gshift = {ref: (0.0, md[ref], 9.0)}
    while len(gshift) < len(gl):
        cand = None
        for (g, h), r in pairw.items():
            v = r.get((md[g], md[h]))
            if not v or not (v[1] >= 4 or (v[2] >= 2 and v[1] >= 1.5)):
                continue
            if g in gshift and h not in gshift:
                o = gshift[g][0] + v[0]
                cand = max(cand, (v[1], h, o), key=lambda t: t[0]) if cand else (v[1], h, o)
            elif h in gshift and g not in gshift:
                o = gshift[h][0] - v[0]
                cand = max(cand, (v[1], g, o), key=lambda t: t[0]) if cand else (v[1], g, o)
        if not cand:
            break
        w, g, o = cand
        gshift[g] = (o, md[g], float(w))
        if groups[g][0]["video"]:
            log(f"  {g}: clock offset {o:+.1f}s ({md[g]} stamps)")
    for g in gl:
        if g not in gshift:
            log(f"  {g.split('|')[0]}{'' if groups[g][0]['video'] else ' ' + os.path.basename(groups[g][0]['path'])}: "
                f"no clear audio match, using its recording time")
            gshift[g] = (0.0, "start", 0.0)
    coarse = {}
    for it in items:
        sh, md, _ = gshift[it["unit"]]
        coarse[it["id"]] = it["guess"][md] + sh
    by_id = {it["id"]: it for it in items}
    for u, v in groups.items():   # audio twins: same start
        if v[0]["video"]:
            continue
        for it in v[1:]:
            coarse[it["id"]] = coarse[v[0]["id"]]
    pos, conf, good = fine_and_solve(items, by_id, coarse)
    # ---- second pass: re-measure each camera clock from its matched clips, retry the rest
    for rnd in range(2):
        weak = [i for i in items if conf[i["id"]] < 1.5]
        if not weak:
            break
        coarse2 = {}
        for g, v in groups.items():
            sh, mdg, w = gshift[g]
            ok = [pos[i["id"]] - i["guess"][mdg] for i in v if conf[i["id"]] >= 1.5]
            if ok:
                ok = sorted(ok)[len(ok) // 4: max(len(ok) // 4 + 1, 3 * len(ok) // 4 + 1)]
                gshift[g] = (float(np.median(ok)), mdg, w)
        for it in items:
            sh, mdg, _ = gshift[it["unit"]]
            coarse2[it["id"]] = pos[it["id"]] if conf[it["id"]] >= 1.5 else it["guess"][mdg] + sh
        log(f"  retrying {len(weak)} clip(s) with corrected camera clocks")
        pos, conf, good = fine_and_solve(items, by_id, coarse2)
        coarse = coarse2
    return pos, conf, coarse, gshift, good


def fine_and_solve(items, by_id, coarse, slack=20.0):
    edges = []
    ids = sorted(by_id, key=lambda i: coarse[i])
    pairs = []
    for ai, a in enumerate(ids):
        A = by_id[a]
        for b in ids[ai + 1:]:
            B = by_id[b]
            if coarse[b] > coarse[a] + A["duration"] + slack:
                break
            if A["unit"] == B["unit"]:
                continue
            ov = min(coarse[a] + A["duration"], coarse[b] + B["duration"]) - max(coarse[a], coarse[b])
            if ov < 4:
                continue
            pairs.append((a, b))
    log(f"  fine-matching {len(pairs)} overlapping pairs")
    for k, (a, b) in enumerate(pairs):
        if k % 20 == 0:
            progress("sync", k, len(pairs))
        e = fine_pair(feats_g[a], feats_g[b], coarse[b] - coarse[a], slack)
        if e:
            edges.append((a, b, e[0], e[1]))
    good = [e for e in edges if e[3] >= 1.2]
    units = {}
    for i in ids:
        units.setdefault(by_id[i]["unit"], []).append(i)
    ties = []
    for u, v in units.items():
        if len(v) > 1 and not by_id[v[0]]["video"]:
            ties += [(v[0], x, 0.0, 4.0) for x in v[1:]]
    pos = solve(ids, good + ties, coarse)
    conf = {i: 0.0 for i in ids}
    deg = {i: 0 for i in ids}
    for a, b, lag, c in good:
        conf[a] = max(conf[a], c)
        conf[b] = max(conf[b], c)
    for u, v in units.items():
        if len(v) > 1 and not by_id[v[0]]["video"]:
            m = max(conf[i] for i in v)
            for i in v:
                conf[i] = m
    return pos, conf, good


def twin_stem(it):
    """Recorders write twins of one take (_MIX/_3, _D safety track, _L/_R). Same stem + same length = same take."""
    b = os.path.splitext(os.path.basename(it["path"]))[0]
    b = re.sub(r"(?i)[_\- ](mix|d|tr\d+|\d|l|r|ms|lr|safety|backup)$", "", b)
    return f"{b}|{round(it['duration'])}"


def fine_pair(fa, fb, guess, slack):
    """Lag (seconds) of b relative to a, searched within guess +- slack."""
    if len(fa) < FPS_FEAT * 3 or len(fb) < FPS_FEAT * 3:
        return None
    r = FPS_FEAT
    if len(fb) > len(fa) + 2 * slack * r + r:      # long recorder as b: flip so we crop it
        e = fine_pair(fb, fa, -guess, slack)
        return (-e[0], e[1]) if e else None
    # cut a to the region that can overlap b
    a0 = int(max(0, (guess - slack) * r))
    a1 = int(min(len(fa), (guess + slack) * r + len(fb)))
    if a1 - a0 < r * 3:
        return None
    sa = fa[a0:a1]
    c, z0 = xcorr(sa - sa.mean(), fb - fb.mean())
    center = z0 - a0   # index of lag 0 in absolute terms of fa
    lo = int(center + (guess - slack) * r)
    hi = int(center + (guess + slack) * r) + 1
    lag, conf = peak_conf(c, center, lo, hi, r)
    if lag is None:
        return None
    # normalize by overlap so long-overlap pairs are not over-trusted
    return lag, conf


def solve(ids, edges, coarse):
    idx = {i: k for k, i in enumerate(ids)}
    # connected components over good edges
    parent = list(range(len(ids)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b, _, _ in edges:
        parent[find(idx[a])] = find(idx[b])
    comps = {}
    for i in ids:
        comps.setdefault(find(idx[i]), []).append(i)
    pos = dict(coarse)
    E = list(edges)
    for comp in comps.values():
        if len(comp) == 1:
            continue
        cset = set(comp)
        ce = [e for e in E if e[0] in cset]
        loc = {i: k for k, i in enumerate(comp)}
        for _ in range(6):
            n = len(comp)
            rows, rhs, w = [], [], []
            for a, b, lag, c in ce:
                r = np.zeros(n)
                r[loc[b]] = 1
                r[loc[a]] = -1
                rows.append(r)
                rhs.append(lag)
                w.append(min(c, 4.0))
            # weak pull toward the coarse guess keeps the system well-posed
            for i in comp:
                r = np.zeros(n)
                r[loc[i]] = 1
                rows.append(r)
                rhs.append(coarse[i])
                w.append(1e-4)
            Aa = np.array(rows) * np.sqrt(np.array(w))[:, None]
            bb = np.array(rhs) * np.sqrt(np.array(w))
            x = np.linalg.lstsq(Aa, bb, rcond=None)[0]
            res = [abs((x[loc[b]] - x[loc[a]]) - lag) for a, b, lag, c in ce]
            worst = int(np.argmax(res)) if res else -1
            if worst < 0 or res[worst] < 0.06:
                break
            ce.pop(worst)
        # keep the whole component near the recording times
        sh = np.median([coarse[i] - x[loc[i]] for i in comp])
        for i in comp:
            pos[i] = float(x[loc[i]] + sh)
    return pos


# ---------------------------------------------------------------- blocks
LAV_WORDS = ("officiant", "pastor", "priest", "rabbi", "groom", "bride", "lav", "lapel", "wireless", "rode", "tx")
DJ_WORDS = ("dj", "board", "band", "mixer", "sound", "reception")


def _cover(files, pos, a, b):
    """Seconds of [a,b] covered by at least one of these files."""
    iv = sorted((max(a, pos[f["id"]]), min(b, pos[f["id"]] + f["duration"])) for f in files)
    tot, cur = 0.0, a
    for s_, e_ in iv:
        if e_ <= s_ or e_ <= cur:
            continue
        tot += e_ - max(s_, cur)
        cur = e_
    return tot


def find_ceremony(items, pos):
    """2+ cameras rolling long takes at the same time while lav mics record = the ceremony."""
    vids = [i for i in items if i["video"]]
    lavs = [i for i in items if not i["video"] and any(w in i["group"].lower() for w in LAV_WORDS)]
    longc = [i for i in vids if i["duration"] >= 8 * 60]
    ev = sorted({pos[i["id"]] for i in longc} | {pos[i["id"]] + i["duration"] for i in longc})
    segs = []
    for a, b in zip(ev, ev[1:]):
        m = (a + b) / 2
        cams = {i["group"] for i in longc if pos[i["id"]] <= m < pos[i["id"]] + i["duration"]}
        if len(cams) >= 2:
            if segs and a - segs[-1][1] < 180:
                segs[-1][1] = b
            else:
                segs.append([a, b])
    best = None
    for a, b in segs:
        L = b - a
        if L < 6 * 60:
            continue
        lav = _cover(lavs, pos, a, b) / L if lavs else 0.0
        sc = 3 * lav + (1 if L <= 100 * 60 else 0) + L / 3600
        if best is None or sc > best[0]:
            best = (sc, a, b, lav)
    if not best or (lavs and best[3] < 0.3):
        return None
    _, a, b, lav = best
    # extend to the long takes that make it up (processional often starts on one camera)
    inv = [i for i in longc if pos[i["id"]] < b and pos[i["id"]] + i["duration"] > a]
    a = min(a, min(pos[i["id"]] for i in inv))
    b = max(b, max(pos[i["id"]] + i["duration"] for i in inv))
    return a, b, lav


def find_blocks(items, pos, gap_min=12 * 60, min_len=90):
    iv = sorted((pos[i["id"]], pos[i["id"]] + i["duration"]) for i in items if i["video"])
    blocks = []
    for a, b in iv:
        if blocks and a - blocks[-1][1] <= gap_min:
            blocks[-1][1] = max(blocks[-1][1], b)
        else:
            blocks.append([a, b])
    cer = find_ceremony(items, pos)
    if cer:   # cut the ceremony out of whatever block it sits in
        ca, cb, _ = cer
        nb = []
        for a, b in blocks:
            if b <= ca or a >= cb:
                nb.append([a, b])
                continue
            if ca - a > 60:
                nb.append([a, ca])
            nb.append([max(a, ca), min(b, cb), "cer"])
            if b - cb > 60:
                nb.append([cb, b])
        blocks = nb
    djs = [i for i in items if not i["video"] and any(w in i["group"].lower() for w in DJ_WORDS)]
    lavs = [i for i in items if not i["video"] and any(w in i["group"].lower() for w in LAV_WORDS)]
    out = []
    for bl in blocks:
        a, b = bl[0], bl[1]
        vids = [i for i in items if i["video"] and pos[i["id"]] < b and pos[i["id"]] + i["duration"] > a]
        cams = sorted({i["group"] for i in vids}, key=natural)
        cover = {c: _cover([i for i in vids if i["group"] == c], pos, a, b) for c in cams}
        L = max(b - a, 1)
        out.append({"start": a, "end": b, "cams": cams, "cover": cover, "n": len(vids),
                    "lav": _cover(lavs, pos, a, b) / L, "dj": _cover(djs, pos, a, b) / L,
                    "is_cer": len(bl) > 2})
    ci = next((k for k, bl in enumerate(out) if bl["is_cer"]), None)
    if ci is None and out:   # no long takes found: lav-heavy multi-camera block, else the biggest one
        ci = max(range(len(out)), key=lambda k: (len(out[k]["cams"]) >= 2, out[k]["lav"] >= 0.3,
                                                  out[k]["end"] - out[k]["start"]))
    for k, bl in enumerate(out):
        L = bl["end"] - bl["start"]
        bl["note"] = ""
        if L < min_len:
            bl["label"] = "Skip"
        elif k == ci:
            bl["label"] = "Ceremony"
        elif ci is not None and k < ci:
            bl["label"], bl["note"] = "Skip", "before the ceremony"
        elif ci is not None and k > ci:
            if not djs or bl["dj"] >= 0.3:
                bl["label"] = "Reception"
            else:
                bl["label"], bl["note"] = "Skip", "cocktail hour?" if k == ci + 1 else "no DJ audio"
        else:
            bl["label"] = "Skip"
    return out


def find_moments(items, pos, join_gap=150, min_len=40):
    """Every stretch where 2+ cameras rolled together. Each gets a guessed name and style;
    you check off the ones to sync and rename them however you like."""
    vids = [i for i in items if i["video"]]
    ev = sorted({pos[i["id"]] for i in vids} | {pos[i["id"]] + i["duration"] for i in vids})
    segs = []
    for a, b in zip(ev, ev[1:]):
        m = (a + b) / 2
        cams = {i["group"] for i in vids if pos[i["id"]] <= m < pos[i["id"]] + i["duration"]}
        if len(cams) >= 2:
            if segs and a - segs[-1][1] <= join_gap:
                segs[-1][1] = b
            else:
                segs.append([a, b])
    cer = find_ceremony(items, pos)
    if cer:   # the ceremony is one moment, start to end
        ca, cb, _ = cer
        inside = [sg for sg in segs if sg[1] > ca - join_gap and sg[0] < cb + join_gap]
        segs = [sg for sg in segs if sg not in inside]
        segs.append([min([ca] + [sg[0] for sg in inside]), max([cb] + [sg[1] for sg in inside]), "cer"])
        segs.sort(key=lambda sg: sg[0])
    segs = [sg for sg in segs if sg[1] - sg[0] >= min_len]
    djs = [i for i in items if not i["video"] and any(w in i["group"].lower() for w in DJ_WORDS)]
    lavs = [i for i in items if not i["video"] and any(w in i["group"].lower() for w in LAV_WORDS)]
    out = []
    for sg in segs:
        a, b = sg[0], sg[1]
        inv = [i for i in vids if pos[i["id"]] < b and pos[i["id"]] + i["duration"] > a]
        cams = sorted({i["group"] for i in inv}, key=natural)
        L = max(b - a, 1)
        aud = sorted({i["group"] for i in items if not i["video"]
                      and pos[i["id"]] < b and pos[i["id"]] + i["duration"] > a}, key=natural)
        out.append({"start": a, "end": b, "cams": cams, "audio": aud, "n": len(inv),
                    "cover": {c: _cover([i for i in inv if i["group"] == c], pos, a, b) for c in cams},
                    "lav": _cover(lavs, pos, a, b) / L, "dj": _cover(djs, pos, a, b) / L,
                    "is_cer": len(sg) > 2})
    ci = next((k for k, m in enumerate(out) if m["is_cer"]), None)
    if ci is None and out:   # no long takes: lav-heavy, then longest multi-camera stretch
        ci = max(range(len(out)), key=lambda k: (out[k]["lav"] >= 0.3, out[k]["end"] - out[k]["start"]))
        out[ci]["is_cer"] = True
    firsts = 0
    for k, m in enumerate(out):
        L = m["end"] - m["start"]
        m["name"], m["style"], m["checked"], m["note"] = f"Moment at {clock(m['start'])}", "ceremony", False, ""
        if k == ci:
            m["name"], m["checked"] = "Ceremony", True
        elif ci is not None and k > ci and (not djs or m["dj"] >= 0.3):
            m["name"], m["style"], m["checked"] = "Reception", "party", True
            m["note"] = "DJ audio" if djs else ""
        elif ci is not None and k < ci and m["lav"] >= 0.3 and L >= 90:
            firsts += 1
            m["name"], m["checked"] = ("First look" if firsts == 1 else f"First look {firsts}"), True
            m["note"] = "lav mics rolling"
        elif ci is not None and k > ci:
            m["note"] = "cocktail hour?" if k == ci + 1 else "no DJ audio"
        else:
            m["note"] = "before the ceremony" if ci is not None and k < ci else ""
        if L < 90 and k != ci:
            m["checked"] = False
    return out


def camera_previews(items, pos, moments, out_dir, ffmpeg, samples=4):
    """A frame per camera per moment, plus a guess at what each camera was doing:
    Roaming (moves around, many short clips), Wide (small faces / whole scene), Close-up."""
    os.makedirs(out_dir, exist_ok=True)
    try:
        import cv2
        face = cv2.CascadeClassifier(os.path.join(HERE, "haarcascade_frontalface_default.xml"))
        if face.empty():
            face = None
    except Exception:
        cv2, face = None, None
    total = sum(len(m["cams"]) for m in moments)
    done = 0
    for k, m in enumerate(moments):
        m["previews"] = {}
        for cam in m["cams"]:
            done += 1
            progress("preview", done, total)
            clips = sorted([i for i in items if i["video"] and i["group"] == cam
                            and pos[i["id"]] < m["end"] and pos[i["id"]] + i["duration"] > m["start"]],
                           key=lambda i: pos[i["id"]])
            if not clips:
                continue
            # sample times spread over this camera's footage in the moment
            spans = [(max(m["start"], pos[c["id"]]), min(m["end"], pos[c["id"]] + c["duration"]), c) for c in clips]
            tot = sum(b - a for a, b, _ in spans)
            want = [tot * (j + 0.5) / samples for j in range(samples)]
            grabs, acc = [], 0.0
            for a, b, c in spans:
                for w in want:
                    if acc <= w < acc + (b - a):
                        grabs.append((c, a + (w - acc) - pos[c["id"]]))
                acc += b - a
            frames = []
            safe = re.sub(r"[^A-Za-z0-9]+", "_", cam)
            for j, (c, t) in enumerate(grabs):
                outp = os.path.join(out_dir, f"m{k}_{safe}_{j}.jpg")
                if not os.path.exists(outp):
                    subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.2f}", "-i", c["path"],
                                    "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", "-y", outp],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
                if os.path.exists(outp):
                    frames.append(outp)
            if not frames:
                continue
            mid = frames[len(frames) // 2]
            guess, role = "", ""
            n_clips, med = len(clips), float(np.median([c["duration"] for c in clips]))
            roam = n_clips >= 5 and med < 90
            sizes, faces_n, diffs = [], [], []
            if cv2 is not None:
                prev = None
                for fp in frames:
                    img = cv2.imread(fp)
                    if img is None:
                        continue
                    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                    small = cv2.resize(cv2.equalizeHist(g), (64, 36)).astype(np.float32)
                    if prev is not None:
                        diffs.append(float(np.abs(small - prev).mean()))
                    prev = small
                    if face is not None:
                        fs = face.detectMultiScale(g, 1.1, 5, minSize=(12, 12))
                        faces_n.append(len(fs))
                        if len(fs):
                            sizes.append(max(h for (_, _, _, h) in fs) / g.shape[0])
                if diffs and np.median(diffs) > 30 and n_clips >= 3:
                    roam = True
            if roam:
                guess, role = "Roaming", "gimbal"
            elif sizes and np.median(sizes) >= 0.14:
                guess = "Close-up"
            elif (sizes and np.median(sizes) < 0.05) or (faces_n and np.mean(faces_n) >= 3):
                guess, role = "Wide", "wide"
            m["previews"][cam] = {"img": mid, "guess": guess, "role": role}
    # same camera folder: keep one guess across the wedding (roaming in any moment = roaming)
    for cam in {c for m in moments for c in m["cams"]}:
        rs = [m["previews"][cam]["role"] for m in moments if cam in m.get("previews", {})]
        if "gimbal" in rs:
            for m in moments:
                if cam in m.get("previews", {}):
                    m["previews"][cam].update(guess="Roaming", role="gimbal")
    return moments


# ---------------------------------------------------------------- FCPXML
def tc_seconds(tc, fps):
    if not tc:
        return Fraction(0)
    m = re.match(r"(\d+):(\d+):(\d+)([:;.])(\d+)", tc)
    if not m:
        return Fraction(0)
    h, mi, s, sep, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), int(m.group(5))
    fps = Fraction(fps)
    nom = int(round(float(fps)))
    frames = ((h * 60 + mi) * 60 + s) * nom + f
    if sep == ";" and nom in (30, 60):
        drop = 2 if nom == 30 else 4
        tm = h * 60 + mi
        frames -= drop * (tm - tm // 10)
    fd = 1 / Fraction(fps) if Fraction(fps).denominator != 1001 else Fraction(1001, nom * 1000)
    return frames * fd


def fstr(fr):
    fr = Fraction(fr)
    return f"{fr.numerator}s" if fr.denominator == 1 else f"{fr.numerator}/{fr.denominator}s"


def file_url(p):
    return "file://" + quote(os.path.abspath(p))


def snapf(sec, fd):
    return round(Fraction(sec).limit_denominator(10 ** 7) / fd) * fd


def fmt_name(w, h, fps):
    f = Fraction(fps)
    rate = {Fraction(24000, 1001): "2398", Fraction(30000, 1001): "2997", Fraction(60000, 1001): "5994"}.get(
        f, str(int(round(float(f)))))
    hh = {2160: "2160p", 1080: "1080p", 720: "720p"}.get(h, f"{h}p")
    if w == 4096:
        hh = "4K"
    return f"FFVideoFormat{hh}{rate}" if h in (2160, 1080, 720) and w in (3840, 1920, 1280) else None


def build_section(sec_name, couple, items, pos, a, b, out_path, default_audio=None):
    """Write an FCPXML with one multicam (Camera groups first, then audio roles) and one project."""
    vids = [i for i in items if i["video"]]
    # multicam format: the most common camera format
    from collections import Counter
    cnt = Counter()
    for i in vids:   # each camera votes once (weighted by its footage), not each clip
        cnt[(i["w"], i["h"], i["fps"])] += i["duration"] / max(1, sum(x["duration"] for x in vids if x["group"] == i["group"]))
    (W, H, FPS), _ = cnt.most_common(1)[0]
    fd = 1 / Fraction(FPS) if Fraction(FPS).denominator != 1001 else Fraction(1001, int(round(float(Fraction(FPS)))) * 1000)
    root = ET.Element("fcpxml", {"version": "1.10"})
    res = ET.SubElement(root, "resources")
    fmts = {}

    def fmt_id(w, h, fps):
        key = (w, h, fps)
        if key not in fmts:
            fid = f"r{len(fmts) + 1}"
            f = Fraction(fps)
            ffd = 1 / f if f.denominator != 1001 else Fraction(1001, int(round(float(f))) * 1000)
            attrs = {"id": fid, "frameDuration": fstr(ffd), "width": str(w), "height": str(h),
                     "colorSpace": "1-1-1 (Rec. 709)"}
            nm = fmt_name(w, h, fps)
            if nm:
                attrs["name"] = nm
            ET.SubElement(res, "format", attrs)
            fmts[key] = fid
        return fmts[key]
    mc_fmt = fmt_id(W, H, FPS)
    for i in vids:
        fmt_id(i["w"], i["h"], i["fps"])
    aud_fmt = None
    nid = [100]

    def new_id():
        nid[0] += 1
        return f"r{nid[0]}"
    for it in items:
        it["_aid"] = new_id()
        if it["video"]:
            # Final Cut reads timecode from .mov/.mxf, but treats .mp4 (Sony XAVC S etc.) as starting at 0
            ext = os.path.splitext(it["path"])[1].lower()
            st = tc_seconds(it.get("timecode"), it["fps"]) if ext in (".mov", ".mxf") else Fraction(0)
            ffd = 1 / Fraction(it["fps"]) if Fraction(it["fps"]).denominator != 1001 else \
                Fraction(1001, int(round(float(Fraction(it["fps"])))) * 1000)
            dur = int(Fraction(it["duration"]).limit_denominator(10 ** 6) / ffd) * ffd
        else:
            rate = it.get("rate") or 48000
            tr = it.get("time_reference")
            st = Fraction(int(tr), rate) if tr and str(tr).isdigit() else Fraction(0)
            dur = Fraction(int(it["duration"] * rate) - rate // 50, rate)
        it["_start"], it["_dur"] = st, dur
        attrs = {"id": it["_aid"], "name": os.path.splitext(os.path.basename(it["path"]))[0],
                 "start": fstr(st), "duration": fstr(dur),
                 "hasVideo": "1" if it["video"] else "0", "hasAudio": "1" if it["audio"] else "0"}
        if it["video"]:
            attrs["format"] = fmts[(it["w"], it["h"], it["fps"])]
            attrs["videoSources"] = "1"
        if it["audio"]:
            attrs.update({"audioSources": str(it.get("audio_streams", 1)), "audioChannels": str(it.get("channels", 2)),
                          "audioRate": str(it.get("rate", 48000))})
        el = ET.SubElement(res, "asset", attrs)
        ET.SubElement(el, "media-rep", {"kind": "original-media", "src": file_url(it["path"])})
    # ---- angles
    mc_id = new_id()
    media = ET.SubElement(res, "media", {"id": mc_id, "name": f"{sec_name} - {couple} Multicam",
                                         "uid": hashlib.md5(f"autocut-sync|{couple}|{sec_name}".encode()).hexdigest().upper()})
    mc = ET.SubElement(media, "multicam", {"format": mc_fmt, "tcStart": "0s", "tcFormat": "NDF"})
    groups = []
    for it in sorted(items, key=lambda i: (not i["video"], natural(i["group"]))):
        if it["group"] not in groups:
            groups.append(it["group"])
    angles = []
    for g in groups:
        clips = sorted([i for i in items if i["group"] == g], key=lambda i: pos[i["id"]])
        # recorder twins (_MIX/_3, _D safety) each get their own angle, named by their suffix
        def suf(c):
            m = re.search(r"(?i)[_\- ](mix|d|\d|l|r|tr\d+)$", os.path.splitext(os.path.basename(c["path"]))[0])
            return m.group(1).upper() if (m and not c["video"]) else ""
        bysuf = {}
        for c in clips:
            bysuf.setdefault(suf(c), []).append(c)
        if len(bysuf) == 1:
            bysuf = {"": clips}
        order = sorted(bysuf, key=lambda k: (k != "MIX", k != "", k))
        for sk in order:
            lanes = []   # split overlapping files of one group into extra angles
            for c in bysuf[sk]:
                for ln in lanes:
                    last = ln[-1]
                    if pos[c["id"]] >= pos[last["id"]] + float(last["_dur"]) - 0.25:
                        ln.append(c)
                        break
                else:
                    lanes.append([c])
            base = g if not sk else f"{g} {sk}"
            for k, ln in enumerate(lanes):
                angles.append((base if k == 0 else f"{base} ({k + 1})", ln))
    total_end = b - a
    t_mc_end = Fraction(0)
    for an, (gname, clips) in enumerate(angles):
        ang = ET.SubElement(mc, "mc-angle", {"name": gname, "angleID": hashlib.md5(f"{couple}{sec_name}{gname}".encode()).hexdigest()[:20]})
        cursor = Fraction(0)
        for c in clips:
            true_off = Fraction(pos[c["id"]] - a).limit_denominator(10 ** 7)
            src_in = Fraction(0)
            if c["video"]:
                off = snapf(true_off, fd)
            else:   # audio: start on the next frame and trim the residual at sample accuracy
                rate = c.get("rate") or 48000
                off = -((-true_off) // fd) * fd
                src_in = Fraction(round((off - true_off) * rate), rate)
            dur = c["_dur"] - src_in
            if off < cursor:              # tiny overlap after snapping, or starts before the section
                cut = cursor - off
                src_in += cut
                dur -= cut
                off = cursor
            dur = min(dur, Fraction(b - a).limit_denominator(10 ** 6) - off)
            dur = int(dur / fd) * fd
            if dur <= 0:
                continue
            if off > cursor:
                ET.SubElement(ang, "gap", {"name": "Gap", "offset": fstr(cursor), "duration": fstr(off - cursor),
                                           "start": "3600s"})
            attrs = {"ref": c["_aid"], "offset": fstr(off), "name": os.path.splitext(os.path.basename(c["path"]))[0],
                     "start": fstr(c["_start"] + src_in), "duration": fstr(dur)}
            if c["video"]:
                attrs["format"] = fmts[(c["w"], c["h"], c["fps"])]
                attrs["tcFormat"] = "NDF"
            else:
                attrs["audioRole"] = "dialogue"
            ET.SubElement(ang, "asset-clip", attrs)
            cursor = off + dur
        t_mc_end = max(t_mc_end, cursor)
        c0 = [x for x in clips if x["video"]]
        if c0:
            angles[an] = (gname, clips, ang.get("angleID"), True)
        else:
            angles[an] = (gname, clips, ang.get("angleID"), False)
    # ---- browser clip + project
    lib = ET.SubElement(root, "library")
    ev = ET.SubElement(lib, "event", {"name": f"{couple} - Multicam Flow Sync"})
    vid_angle = next(x for x in angles if x[3])
    aud = None
    if default_audio:
        aud = next((x for x in angles if x[0] == default_audio), None)
    if aud is None:
        dj = ("dj", "board", "mixer", "band")
        if sec_name.lower().startswith("cer"):
            tiers = [dj, ("officiant", "pastor", "priest", "minister"), ("groom", "bride"),
                     ("lav", "lapel", "wireless", "rode", "tx", "mic"), ("podium",)]
        else:
            tiers = [dj, ("podium", "toast", "mic"), ("lav", "lapel", "wireless")]
        for words in tiers:
            hits = [x for x in angles if not x[3] and any(w in x[0].lower() for w in words)]
            if hits:   # the one that covers the most of this section
                aud = max(hits, key=lambda x: ("mix" in x[0].lower(), sum(float(c["_dur"]) for c in x[1]), -len(x[0])))
                break
    aud = aud or vid_angle
    total = int(t_mc_end / fd) * fd
    ET.SubElement(ev, "mc-clip", {"ref": mc_id, "offset": "0s", "name": media.get("name"), "duration": fstr(total)})
    proj = ET.SubElement(ev, "project", {"name": f"{sec_name} - {couple}"})
    seq = ET.SubElement(proj, "sequence", {"format": mc_fmt, "duration": fstr(total), "tcStart": "0s",
                                           "tcFormat": "NDF", "audioLayout": "stereo", "audioRate": "48k"})
    spine = ET.SubElement(seq, "spine")
    mcc = ET.SubElement(spine, "mc-clip", {"ref": mc_id, "offset": "0s", "name": media.get("name"),
                                           "start": "0s", "duration": fstr(total)})
    if aud[2] == vid_angle[2]:
        ET.SubElement(mcc, "mc-source", {"angleID": vid_angle[2], "srcEnable": "all"})
    else:
        ET.SubElement(mcc, "mc-source", {"angleID": vid_angle[2], "srcEnable": "video"})
        ET.SubElement(mcc, "mc-source", {"angleID": aud[2], "srcEnable": "audio"})
    ET.indent(root, space="  ")
    with open(out_path, "wb") as fh:
        fh.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n')
        fh.write(ET.tostring(root, encoding="utf-8"))
    return {"angles": [x[0] for x in angles], "audio": aud[0], "video": vid_angle[0], "duration": float(total)}


def natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def couple_name(folder):
    b = os.path.basename(os.path.normpath(folder))
    b = re.sub(r"^\d{6,8}[_\- ]*", "", b)
    parts = [p for p in re.split(r"[_\-]+|\s+and\s+|\s*&\s*", b) if p]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return b.replace("_", " ").strip() or "Wedding"


def ts(sec):
    sec = max(0, sec)
    return f"{int(sec // 3600)}:{int(sec % 3600 // 60):02d}:{int(sec % 60):02d}"


def clock(t):
    try:
        return datetime.fromtimestamp(t).strftime("%-I:%M %p")
    except Exception:
        return "?"


# ---------------------------------------------------------------- commands
def cmd_scan(folder, out_dir, workers=3):
    global LOG_FH
    os.makedirs(out_dir, exist_ok=True)
    LOG_FH = open(os.path.join(out_dir, "sync.log"), "w")
    ffmpeg = find_ffmpeg()
    cache = os.path.join(DATA, "Sync Cache")
    os.makedirs(cache, exist_ok=True)
    files = walk_media(folder)
    log(f"Found {len(files)} media files in {folder}")
    if not files:
        sys.exit("No video or audio files found in that folder.")
    infos = []
    for k, f in enumerate(files):
        progress("scan", k, len(files))
        try:
            infos.append(probe_cached(ffmpeg, f, cache))
        except Exception as e:
            log(f"  skip {f}: {e}")
    infos = [i for i in infos if i["duration"] >= 1.0 and (i["video"] or i["audio"])]
    # groups
    for i in infos:
        i["group"] = group_for(i["path"], folder, i["video"], i["model"])
    loose = [i for i in infos if i["group"] is None]
    if loose:
        models = {i["model"] for i in loose if i["model"]}
        for i in loose:
            if len(models) > 1 and i["model"]:
                i["group"] = i["model"]
            else:
                i["group"] = name_prefix(i["path"])
        # one prefix only? then everything is one camera
    for k, i in enumerate(infos):
        i["id"] = f"f{k}"
        end = i["ctime"]
        if i["ctime"] is None:
            # file modified time is usually when recording stopped
            i["guess"] = {"start": i["mtime"] - i["duration"], "end": i["mtime"] - i["duration"]}
        else:
            i["guess"] = {"start": i["ctime"], "end": i["ctime"] - i["duration"]}
    groups = {}
    for i in infos:
        groups.setdefault(i["group"], []).append(i)
    log("Cameras and audio:")
    for g, v in sorted(groups.items(), key=lambda x: (not x[1][0]["video"], natural(x[0]))):
        kind = "video" if v[0]["video"] else "audio"
        log(f"  {g}: {len(v)} {kind} files, {sum(x['duration'] for x in v) / 60:.0f} min")
    # features
    with_audio = [i for i in infos if i["audio"]]
    feats = {}
    done = [0]

    def work(i):
        f = onset_feature(ffmpeg, i["path"], cache, cache_key(i), i)
        done[0] += 1
        progress("listen", done[0], len(with_audio))
        return i["id"], f
    log("Listening to every file (first time on a wedding takes a while; it's cached after)...")
    with ThreadPoolExecutor(workers) as ex:
        for k, f in ex.map(work, with_audio):
            feats[k] = f
    items = [i for i in infos if i["audio"] and len(feats.get(i["id"], [])) > FPS_FEAT * 2]
    silent = [i for i in infos if i not in items]
    log("Syncing by audio...")
    pos, conf, coarse, gshift, edges = sync(items, feats)
    for i in silent:   # files with no usable audio: recording time + their camera's clock offset
        sh, md, _ = gshift.get(i["group"], (0.0, "start", 0))
        pos[i["id"]] = i["guess"][md] + sh
        conf[i["id"]] = 0.0
    def others_rolling(i):
        a, b = pos[i["id"]], pos[i["id"]] + i["duration"]
        return any(j is not i and j["audio"] and j["group"] != i["group"] and
                   min(b, pos[j["id"]] + j["duration"]) - max(a, pos[j["id"]]) > min(5.0, i["duration"] / 2)
                   for j in items)
    weak = [i for i in infos if i["video"] and conf.get(i["id"], 0) < 1.0 and others_rolling(i)]
    blocks = find_blocks(infos, pos)
    moments = find_moments(infos, pos)
    log(f"\nMulti-camera moments: {len(moments)}")
    for m in moments:
        log(f"  {clock(m['start'])} to {clock(m['end'])} ({(m['end'] - m['start']) / 60:.0f} min) "
            f"{', '.join(m['cams'])} -> {m['name']}{' [x]' if m['checked'] else ''}")
    try:
        camera_previews(infos, pos, moments, os.path.join(out_dir, "previews"), ffmpeg)
    except Exception as e:
        log(f"  (camera previews skipped: {e})")
    log(f"\nThe day splits into {len(blocks)} block(s):")
    for k, bl in enumerate(blocks, 1):
        log(f"  {k}. {clock(bl['start'])} to {clock(bl['end'])} ({(bl['end'] - bl['start']) / 60:.0f} min), "
            f"cameras: {', '.join(bl['cams'])}  -> {bl['label']}{'  (' + bl['note'] + ')' if bl['note'] else ''}")
    silent = [i for i in weak if not i["audio"]]
    weak_s = [i for i in weak if i["audio"]]
    if weak_s:
        log(f"\n{len(weak_s)} clip(s) had nothing to match against (quiet b-roll), placed by the camera's clock "
            f"(usually within a second):")
        for i in weak_s[:30]:
            log(f"  {os.path.relpath(i['path'], folder)}")
    if silent:
        log(f"\n{len(silent)} clip(s) have no sound (drone?), placed by recording time only:")
        for i in silent[:30]:
            log(f"  {os.path.relpath(i['path'], folder)}")
    data = {"folder": folder, "couple": couple_name(folder), "created": time.time(),
            "files": [{k: v for k, v in i.items() if not k.startswith("_")} for i in infos],
            "pos": {k: float(v) for k, v in pos.items()}, "conf": {k: float(v) for k, v in conf.items()},
            "coarse": {k: float(v) for k, v in coarse.items()},
            "clock": {g: [float(v[0]), v[1], float(v[2])] for g, v in gshift.items()}, "blocks": blocks,
            "moments": moments,
            "weak": [i["path"] for i in weak if i["audio"]], "silent": [i["path"] for i in weak if not i["audio"]]}
    out = os.path.join(out_dir, "sync.json")
    json.dump(data, open(out, "w"), indent=1)
    progress("scan", 1, 1)
    print("@scan_done " + out, flush=True)
    return out


def cmd_warm(folder, budget, workers=3, mbps=300.0):
    """Fill the cache for as many files as fit in `budget` seconds. Safe to repeat."""
    ffmpeg = find_ffmpeg()
    cache = os.path.join(DATA, "Sync Cache")
    os.makedirs(cache, exist_ok=True)
    t0 = time.time()
    files = walk_media(folder)
    todo = []
    nprobed = 0
    for f in files:
        if time.time() - t0 > budget * 0.5:
            break
        nprobed += 1
        i = probe_cached(ffmpeg, f, cache)
        if i["audio"] and i["duration"] >= 1.0 and not os.path.exists(os.path.join(cache, cache_key(i) + ".npy")):
            todo.append(i)
    probed = time.time() - t0
    units = []
    for i in todo:
        n = n_parts(i["duration"])
        if n == 1:
            units.append((i, None, i["size"]))
        else:
            for k in range(n):
                if not os.path.exists(part_path(cache, cache_key(i), k)):
                    units.append((i, k, i["size"] / n))
    units.sort(key=lambda u: -u[2])
    done, lock = [0], __import__("threading").Lock()
    lanes = [[] for _ in range(workers)]
    load = [0.0] * workers
    for u in units:   # plan: only start pieces that can finish in the time left
        k = int(np.argmin(load))
        est = u[2] / (mbps * 1e6 / workers) + 3
        if probed + load[k] + est < budget:
            lanes[k].append(u)
            load[k] += est

    def run_lane(ln):
        for i, k, _ in ln:
            if time.time() - t0 > budget:
                return
            if k is None:
                onset_feature(ffmpeg, i["path"], cache, cache_key(i), i)
            else:
                do_part(ffmpeg, i, cache, k)
            with lock:
                done[0] += 1
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(run_lane, lanes))
    # stitch any file whose pieces are all in
    for i in todo:
        n = n_parts(i["duration"])
        if n > 1 and all(os.path.exists(part_path(cache, cache_key(i), k)) for k in range(n)):
            onset_feature(ffmpeg, i["path"], cache, cache_key(i), i)
    todo = units
    left = len(todo) - done[0]
    print(f"@warm files={len(files)} probed={nprobed} done_now={done[0]} "
          f"left={left} secs={time.time() - t0:.0f}", flush=True)


def cmd_build(sync_json, labels=None, dest=None, pad=5.0, selection=None):
    """labels: old style {block#: name}. selection: [{"idx": [moment#...], "name", "style", "roles": {cam: role}}]."""
    global LOG_FH
    data = json.load(open(sync_json))
    LOG_FH = open(os.path.join(os.path.dirname(sync_json), "build.log"), "w")
    dest = dest or os.path.join(DATA, "Inbox")
    os.makedirs(dest, exist_ok=True)
    files = data["files"]
    pos = {k: float(v) for k, v in data["pos"].items()}
    couple = data["couple"]
    sections, meta = {}, {}
    if selection is not None:
        moments = data.get("moments", [])
        for sel in selection:
            name = re.sub(r"[/:\\]+", "-", str(sel.get("name") or "Moment")).strip() or "Moment"
            for k in sel.get("idx", []):
                if 0 <= int(k) < len(moments):
                    sections.setdefault(name, []).append(moments[int(k)])
            meta.setdefault(name, {"style": sel.get("style", "ceremony"), "roles": {}})
            meta[name]["roles"].update({c: r for c, r in (sel.get("roles") or {}).items() if r})
    else:
        blocks = data["blocks"]
        if labels:
            for k, v in labels.items():
                blocks[int(k) - 1]["label"] = v
        for bl in blocks:
            if bl["label"] and bl["label"] != "Skip":
                sections.setdefault(bl["label"], []).append(bl)
    outs, manifest = [], []
    for name, bls in sections.items():
        a = min(b["start"] for b in bls) - pad
        b = max(b["end"] for b in bls) + pad
        inside = [f for f in files if pos[f["id"]] < b and pos[f["id"]] + f["duration"] > a]
        # only include files that fall inside one of the chosen blocks (or audio spanning them)
        keep = []
        for f in inside:
            s, e = pos[f["id"]], pos[f["id"]] + f["duration"]
            if any(s < bl["end"] + pad and e > bl["start"] - pad for bl in bls):
                keep.append(f)
        # audio recorders that run far beyond the section: fine, they get trimmed by the section start
        a = min(a, min(pos[f["id"]] for f in keep if f["video"]) - 0.0)
        a = max(a, min(pos[f["id"]] for f in keep))
        for f in keep:
            f["_shift"] = 0
        # trim to section: clips starting before a get their head cut in build_section
        path = os.path.join(dest, f"{name} - {couple}.fcpxml")
        st = build_section(name, couple, keep, pos, a, b, path)
        log(f"Wrote {path}  ({st['duration'] / 60:.0f} min, angles: {', '.join(st['angles'])}; audio: {st['audio']})")
        outs.append(path)
        mt = meta.get(name, {"style": "party" if name.lower().startswith("recep") else "ceremony", "roles": {}})
        roles = ",".join(f"{c}={r}" for c, r in mt["roles"].items() if c in st["angles"])
        manifest.append({"name": name, "path": path, "style": mt["style"], "roles": roles,
                         "duration": st["duration"], "audio": st["audio"], "angles": st["angles"]})
        print("@built " + path, flush=True)
    json.dump({"couple": couple, "sections": manifest}, open(os.path.join(dest, "manifest.json"), "w"), indent=1)
    return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "build", "all", "warm"])
    ap.add_argument("--budget", type=float, default=160)
    ap.add_argument("target")
    ap.add_argument("--out")
    ap.add_argument("--labels")
    ap.add_argument("--selection", help="JSON list of moments to build (from the window)")
    ap.add_argument("--dest")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    if args.cmd == "warm":
        return cmd_warm(os.path.abspath(args.target), args.budget, args.workers)
    if args.cmd in ("scan", "all"):
        folder = os.path.abspath(args.target)
        out_dir = args.out or os.path.join(DATA, "Work", re.sub(r'[/:\\]+', "-", couple_name(folder)), "_scan")
        sj = cmd_scan(folder, out_dir, args.workers)
        if args.cmd == "all":
            cmd_build(sj, None, args.dest)
    else:
        sel = None
        if args.selection:
            sel = json.load(open(args.selection)) if os.path.exists(args.selection) else json.loads(args.selection)
        cmd_build(args.target, json.loads(args.labels) if args.labels else None, args.dest, selection=sel)


if __name__ == "__main__":
    main()
