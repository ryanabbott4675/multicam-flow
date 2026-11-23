#!/usr/bin/env python3
"""
Multicam Flow for Final Cut Pro multicam (weddings, ceremonies, toasts).

Reads an FCPXML (or .fcpxmld bundle) exported from Final Cut that contains a
synced multicam clip. Scores every angle over time for focus, camera movement
(shake / pans), exposure, faces and on-screen activity. Picks the best usable
angle, keeps audio locked to one angle, and writes a new FCPXML project you can
import back into Final Cut. Every cut stays a live multicam edit you can switch.

Usage:
  python3 engine.py Ceremony.fcpxmld --main "Cam A" --audio "Cam A"

Key options:
  --main NAME        Safe default angle (usually the wide / center camera).
  --audio NAME       Angle whose audio plays the whole time (your clean mic).
  --min-shot SEC     Shortest allowed shot (default 2.5).
  --max-shot SEC     Longest a shot can run before a cutaway is tried (default 10).
  --force A-B=NAME   Force an angle for a time range, e.g. 12:30-12:41="Cam B".
  --range A-B        Only cut part of the multicam, e.g. 05:00-20:00.
  --sheets           Save contact sheets for review.
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from urllib.parse import unquote, urlparse

import numpy as np
import cv2

# ----------------------------------------------------------------- helpers

def ftime(s, default=Fraction(0)):
    """Parse FCPXML time like '1001/24000s', '3600s', '0s'."""
    if s is None:
        return default
    s = s.strip()
    if s.endswith("s"):
        s = s[:-1]
    if "/" in s:
        n, d = s.split("/")
        return Fraction(int(n), int(d))
    return Fraction(s)


def ts(sec):
    sec = float(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}" if h else f"{int(m):02d}:{s:05.2f}"


def parse_clock(s):
    """'12:30' or '1:02:03.5' or '95' -> seconds."""
    parts = [float(p) for p in s.strip().split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def find_ffmpeg():
    env = os.environ.get("AUTOCUT_FFMPEG")
    if env:
        return env
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        sys.exit("ffmpeg not found. Install it or `pip install imageio-ffmpeg`.")


LOG_FH = None


def log(*a):
    print(*a, file=sys.stderr, flush=True)
    if LOG_FH:
        try:
            LOG_FH.write(" ".join(str(x) for x in a) + "\n")
            LOG_FH.flush()
        except Exception:
            pass

# ----------------------------------------------------------------- FCPXML

ROLE_WORDS = [("groom", ("groom", "husband")), ("bride", ("bride", "wife")),
              ("officiant", ("officiant", "pastor", "minister", "celebrant", "priest", "rabbi", "reverend", "judge"))]


def audio_role(path):
    p = path.lower().replace("\\", "/")
    parts = p.split("/")[-3:]  # folder names and file name near the file
    for role, words in ROLE_WORDS:
        if any(w in part for part in parts for w in words):
            return role
    return None


class Project:
    def __init__(self, path, multicam_name=None, use_proxy=True):
        if os.path.isdir(path):
            path = os.path.join(path, "Info.fcpxml")
        self.path = path
        self.tree = ET.parse(path)
        self.root = self.tree.getroot()
        self.version = self.root.get("version", "1.10")
        res = self.root.find("resources")
        self.resources = res
        self.formats = {f.get("id"): f for f in res.findall("format")}
        self.assets = {}
        for a in res.findall("asset"):
            src = None
            reps = {r.get("kind"): r.get("src") for r in a.findall("media-rep")}
            if use_proxy and reps.get("proxy-media"):
                src = reps["proxy-media"]
            src = src or reps.get("original-media") or a.get("src")
            self.assets[a.get("id")] = {
                "src": self._path(src),
                "start": ftime(a.get("start")),
                "duration": ftime(a.get("duration")),
                "hasVideo": a.get("hasVideo", "0") == "1",
                "format": a.get("format"),
                "name": a.get("name"),
            }
        mcs = [m for m in res.findall("media") if m.find("multicam") is not None]
        if not mcs:
            sys.exit("No multicam clip found in this FCPXML. Export the multicam clip "
                     "(or a project that uses it) from Final Cut.")
        self.all_multicams = [m.get("name") for m in mcs]
        if multicam_name:
            mcs = [m for m in mcs if m.get("name") == multicam_name] or sys.exit(
                f"Multicam '{multicam_name}' not found. Have: {self.all_multicams}")
        self.media = mcs[0]
        self.mc = self.media.find("multicam")
        self.mc_tcstart = ftime(self.mc.get("tcStart"))
        fmt = self.formats.get(self.mc.get("format"))
        self.format_id = self.mc.get("format")
        self.frame_dur = ftime(fmt.get("frameDuration")) if fmt is not None and fmt.get("frameDuration") else Fraction(1001, 24000)
        self.angles = []
        for ang in self.mc.findall("mc-angle"):
            clips = self._angle_clips(ang)
            self.angles.append({"name": ang.get("name"), "id": ang.get("angleID"), "clips": clips,
                                "has_video": any(c["video"] for c in clips)})
        self.duration = max((c["off"] + c["dur"] for a in self.angles for c in a["clips"]), default=0)
        self.video_angles = [a for a in self.angles if a["has_video"]]
        # Lav / recorder roles from folder or file names (Audio/Groom/..., officiant_lav.wav)
        self.role_audio = {}
        seen = set()
        for a in self.angles:
            for c in a["clips"]:
                if c["video"] or not c["src"]:
                    continue
                r = audio_role(c["src"])
                key = (c["src"], round(c["off"], 2))
                if r and key not in seen:
                    seen.add(key)
                    self.role_audio.setdefault(r, []).append(c)
        # Read the editor's existing cut of this multicam, if the export has one.
        self.existing = []
        self.project_names = [p.get("name") for p in self.root.iter("project")]
        mid = self.media.get("id")
        for proj in self.root.iter("project"):
            for el in proj.iter("mc-clip"):
                if el.get("ref") != mid:
                    continue
                st = float(ftime(el.get("start")) - self.mc_tcstart)
                du = float(ftime(el.get("duration")))
                v = a_ = None
                for src in el.findall("mc-source"):
                    en = src.get("srcEnable", "all")
                    if en in ("all", "video"):
                        v = src.get("angleID")
                    if en in ("all", "audio"):
                        a_ = src.get("angleID")
                self.existing.append((st, du, v, a_))
        seq = next(self.root.iter("sequence"), None)
        sf = self.formats.get(seq.get("format")) if seq is not None else None
        self.seq_frame = ftime(sf.get("frameDuration")) if sf is not None and sf.get("frameDuration") else self.frame_dur

    search_roots = []
    _index = None

    @classmethod
    def _path(cls, src):
        if not src:
            return None
        p = unquote(urlparse(src).path) if src.startswith("file:") else unquote(src)
        if os.path.exists(p) or not cls.search_roots:
            return p
        # Final Cut paths (/Volumes/Drive/...) can differ from where this machine
        # sees the files. Find the same file by name (and size-free path tail).
        if cls._index is None:
            cls._index = {}
            for root in cls.search_roots:
                for dp, dn, fn in os.walk(root):
                    dn[:] = [d for d in dn if not d.startswith(".") and not d.endswith((".fcpcache", "Render Files", "Analysis Files", "Shared Items"))]
                    for n in fn:
                        cls._index.setdefault(n, []).append(os.path.join(dp, n))
        hits = cls._index.get(os.path.basename(p), [])
        if len(hits) > 1:
            parts = p.split("/")
            hits.sort(key=lambda h: -sum(1 for a, b in zip(reversed(h.split("/")), reversed(parts)) if a == b))
        return hits[0] if hits else p

    def _angle_clips(self, ang):
        """Return video clips: off (sec from multicam start), dur, src file, src_in (sec into file)."""
        out = []
        for el in ang:
            tag = el.tag
            if tag == "asset-clip":
                a = self.assets.get(el.get("ref"))
                if not a:
                    continue
                off = ftime(el.get("offset")) - self.mc_tcstart
                start = ftime(el.get("start"), a["start"])
                dur = ftime(el.get("duration"))
                out.append(self._clip(a, off, start - a["start"], dur))
            elif tag == "clip":
                off = ftime(el.get("offset")) - self.mc_tcstart
                cstart = ftime(el.get("start"))
                dur = ftime(el.get("duration"))
                v = el.find("video")
                if v is None:
                    v = el.find(".//video")
                if v is None:
                    continue
                a = self.assets.get(v.get("ref"))
                if not a:
                    continue
                voff = ftime(v.get("offset"))
                vstart = ftime(v.get("start"), a["start"])
                src_in = vstart + (cstart - voff) - a["start"]
                out.append(self._clip(a, off, src_in, dur))
            elif tag in ("gap", "spine"):
                continue
            else:
                log(f"  note: skipping <{tag}> inside angle '{ang.get('name')}'")
        return out

    def _clip(self, a, off, src_in, dur):
        fmt = self.formats.get(a["format"])
        w = int(fmt.get("width", 1920)) if fmt is not None else 1920
        h = int(fmt.get("height", 1080)) if fmt is not None else 1080
        return {"off": float(off), "dur": float(dur), "src": a["src"], "src_in": float(src_in),
                "w": w, "h": h, "name": a["name"], "video": a["hasVideo"]}

    def angle(self, name):
        if name is None:
            return None
        for a in self.angles:
            if a["name"] == name or a["name"].lower() == name.lower():
                return a
        log(f"  WARNING: angle '{name}' not found, ignoring. Angles: {[a['name'] for a in self.angles]}")
        return None

    def angle_by_id(self, aid):
        return next((a for a in self.angles if a["id"] == aid), None)

    # ---------- writing
    def snap(self, sec):
        frames = round(Fraction(sec).limit_denominator(1000000) / self.frame_dur)
        return frames * self.frame_dur

    @staticmethod
    def fstr(fr):
        fr = Fraction(fr)
        if fr.denominator == 1:
            return f"{fr.numerator}s"
        return f"{fr.numerator}/{fr.denominator}s"

    def write(self, shots, audio_angle, out_path, project_name, t0=0.0, markers=()):
        root = ET.Element("fcpxml", {"version": self.version})
        res = ET.SubElement(root, "resources")
        for child in list(self.resources):
            res.append(child)
        lib = ET.SubElement(root, "library")
        ev = ET.SubElement(lib, "event", {"name": "Multicam Flow"})
        proj = ET.SubElement(ev, "project", {"name": project_name})
        cuts = [(self.snap(s), self.snap(e), a) for s, e, a in shots]
        total = cuts[-1][1] - cuts[0][0]
        seq = ET.SubElement(proj, "sequence", {
            "format": self.format_id, "tcStart": "0s", "tcFormat": self.mc.get("tcFormat", "NDF"),
            "duration": self.fstr(total), "audioLayout": "stereo", "audioRate": "48k"})
        spine = ET.SubElement(seq, "spine")
        pos = Fraction(0)
        total = sum((e - s for s, e, a in cuts if e > s), Fraction(0))
        seq.set("duration", self.fstr(total))
        for s, e, ang in cuts:
            if e <= s:
                continue
            mc = ET.SubElement(spine, "mc-clip", {
                "ref": self.media.get("id"), "name": self.media.get("name", "Multicam"),
                "offset": self.fstr(pos), "start": self.fstr(self.mc_tcstart + s),
                "duration": self.fstr(e - s)})
            pos += e - s
            if audio_angle is None or audio_angle["id"] == ang["id"]:
                ET.SubElement(mc, "mc-source", {"angleID": ang["id"], "srcEnable": "all"})
            else:
                ET.SubElement(mc, "mc-source", {"angleID": audio_angle["id"], "srcEnable": "audio"})
                ET.SubElement(mc, "mc-source", {"angleID": ang["id"], "srcEnable": "video"})
            for mt, label in markers:
                m = self.snap(mt)
                if s <= m < e:
                    # To-do marker (red) so each moment can be checked off in the timeline index.
                    ET.SubElement(mc, "marker", {"start": self.fstr(self.mc_tcstart + m),
                                                 "duration": self.fstr(self.frame_dur),
                                                 "value": label, "completed": "0"})
        ET.indent(root)
        xml = ET.tostring(root, encoding="unicode")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n')
            f.write(xml)


    # ---------- recut in place: keep the whole project, only re-cut the multicam clips
    MARKER_TAGS = ("marker", "chapter-marker", "rating", "keyword", "analysis-marker")

    def write_inplace(self, cut_fn, out_path, markers, suffix=" Multicam Flow", min_shot=2.0, audio_id=None):
        """Recut every multicam clip in the project. Pieces whose angle is GAP (no camera
        has a picture) are removed and the rest of that storyline slides up."""
        import copy
        root = copy.deepcopy(self.root)
        mid = self.media.get("id")
        fd = self.seq_frame
        pairs = []
        for proj in root.iter("project"):
            proj.set("name", (proj.get("name") or "Project") + suffix)
            for parent in proj.iter():
                for child in list(parent):
                    if child.tag == "mc-clip" and child.get("ref") == mid:
                        pairs.append((child, parent))
        marks = sorted(markers)
        stats = {"sections": 0, "pieces": 0, "skipped": 0, "gaps_removed": 0, "gap_seconds": 0.0}
        seq_of = {}
        for proj in root.iter("project"):
            seq = proj.find("sequence")
            if seq is not None:
                sp = seq.find("spine")
                if sp is not None:
                    seq_of[id(sp)] = seq

        def snap_local(x):
            return round(Fraction(x).limit_denominator(1000000) / fd) * fd

        for el, parent in pairs:
            st = ftime(el.get("start"))
            du = ftime(el.get("duration"))
            off = ftime(el.get("offset"))
            # Split edits (J/L cuts): audio can start earlier / end later than the
            # picture. Keep the lead-in on the first piece and the tail on the last.
            a_lo = ftime(el.get("audioStart")) if el.get("audioStart") else st
            a_hi = a_lo + ftime(el.get("audioDuration")) if el.get("audioDuration") else st + du
            rel = float(st - self.mc_tcstart)
            srcs = el.findall("mc-source")
            vsrc = next((x for x in srcs if x.get("srcEnable", "all") in ("all", "video")), None)
            if vsrc is None or du < Fraction(3, 2):
                stats["skipped"] += 1
                continue
            shots = cut_fn(rel, rel + float(du))
            # local boundaries, frame-aligned to the project, sliver-free
            bounds = []
            for a, b, aid in shots:
                la = snap_local(a - rel)
                la = max(Fraction(0), min(la, du))
                if bounds and bounds[-1][1] == aid:
                    continue
                if bounds and la <= bounds[-1][0]:
                    bounds[-1] = (bounds[-1][0], aid)
                    continue
                bounds.append((la, aid))
            if not bounds:
                continue
            bounds[0] = (Fraction(0), bounds[0][1])
            # merge pieces shorter than min_shot into the previous one (never a gap)
            merged = []
            for i, (la, aid) in enumerate(bounds):
                end = bounds[i + 1][0] if i + 1 < len(bounds) else du
                if merged and aid != GAP and merged[-1][1] != GAP and \
                        (end - la) < Fraction(min_shot).limit_denominator(1000) and du > 2 * min_shot:
                    continue
                if merged and merged[-1][1] == aid:
                    continue
                merged.append((la, aid))
            pieces = [(merged[i][0], merged[i + 1][0] if i + 1 < len(merged) else du, merged[i][1])
                      for i in range(len(merged))]
            pieces = [p for p in pieces if p[1] > p[0]]
            children = list(el)
            can_ripple = parent.tag == "spine"
            keep = [p for p in pieces if p[2] != GAP]
            has_anchored = any(c.get("lane") is not None for c in children)
            if not can_ripple or not keep:
                # Can't remove here (inside a connected storyline, or the whole clip is
                # a gap): keep those stretches on the original angle instead.
                pieces = [(la, lb, vsrc.get("angleID") if aid == GAP else aid) for la, lb, aid in pieces]
                keep = pieces
            # anchored children of removed pieces move to the nearest kept piece
            def home(t):
                for la, lb, aid in keep:
                    if st + la <= t < st + lb:
                        return (la, lb)
                return min(((la, lb) for la, lb, aid in keep),
                           key=lambda p: min(abs(t - (st + p[0])), abs(t - (st + p[1]))))
            removed_before = Fraction(0)
            shift_of = {}
            for la, lb, aid in pieces:
                if aid == GAP:
                    removed_before += lb - la
                    stats["gaps_removed"] += 1
                    stats["gap_seconds"] += float(lb - la)
                else:
                    shift_of[(la, lb)] = removed_before
            total_removed = removed_before
            pieces = keep
            idx = list(parent).index(el)
            parent.remove(el)
            new_els = []
            for pi, (la, lb, aid) in enumerate(pieces):
                ne = ET.Element("mc-clip", dict(el.attrib))
                ne.set("offset", self.fstr(off + la - shift_of.get((la, lb), Fraction(0))))
                ne.set("start", self.fstr(st + la))
                ne.set("duration", self.fstr(lb - la))
                lo, hi = st + la, st + lb
                last = pi == len(pieces) - 1
                ne.attrib.pop("audioStart", None)
                ne.attrib.pop("audioDuration", None)
                p_alo = a_lo if pi == 0 else lo
                p_ahi = a_hi if last else hi
                if (p_alo, p_ahi) != (lo, hi):
                    ne.set("audioStart", self.fstr(p_alo))
                    ne.set("audioDuration", self.fstr(p_ahi - p_alo))
                for c in children:
                    if c.tag == "mc-source" and audio_id:
                        # Audio override: the chosen angle becomes the audio for every piece.
                        en = c.get("srcEnable", "all")
                        if en == "audio":
                            a_ = copy.deepcopy(c)
                            a_.set("angleID", audio_id)
                            ne.append(a_)
                            continue
                        if en == "all":
                            a_ = copy.deepcopy(c)
                            a_.set("srcEnable", "audio")
                            a_.set("angleID", audio_id)
                            for f in list(a_):
                                if f.tag.startswith("filter-video"):
                                    a_.remove(f)
                            v = copy.deepcopy(c)
                            v.set("srcEnable", "video")
                            v.set("angleID", aid)
                            for f in list(v):
                                if f.tag.startswith("filter-audio"):
                                    v.remove(f)
                            ne.append(a_)
                            ne.append(v)
                            continue
                    if c.tag == "mc-source":
                        en = c.get("srcEnable", "all")
                        if en == "audio":
                            ne.append(copy.deepcopy(c))
                        elif en == "video":
                            v = copy.deepcopy(c)
                            v.set("angleID", aid)
                            ne.append(v)
                        else:  # one source for both: split so audio stays put
                            if aid == c.get("angleID"):
                                ne.append(copy.deepcopy(c))
                            else:
                                a_ = copy.deepcopy(c)
                                a_.set("srcEnable", "audio")
                                for f in list(a_):
                                    if f.tag.startswith("filter-video"):
                                        a_.remove(f)
                                v = copy.deepcopy(c)
                                v.set("srcEnable", "video")
                                v.set("angleID", aid)
                                for f in list(v):
                                    if f.tag.startswith("filter-audio"):
                                        v.remove(f)
                                ne.append(a_)
                                ne.append(v)
                        continue
                    if c.get("lane") is not None:  # connected clip / title / adjustment layer
                        t = ftime(c.get("offset"))
                        h = home(t)
                        if h == (la, lb):
                            cc = copy.deepcopy(c)
                            if not (lo <= t < hi):
                                cc.set("offset", self.fstr(min(max(t, lo), hi - fd)))
                            ne.append(cc)
                        continue
                    if c.tag in self.MARKER_TAGS:
                        t = ftime(c.get("start"))
                        if home(t) == (la, lb):
                            cc = copy.deepcopy(c)
                            if not (lo <= t < hi):
                                cc.set("start", self.fstr(min(max(t, lo), hi - fd)))
                            ne.append(cc)
                        continue
                    cc = copy.deepcopy(c)  # effects, volume, conform-rate: every piece
                    for f in list(cc.iter()):
                        for g in list(f):
                            if (g.tag == "fadeIn" and pi > 0) or (g.tag == "fadeOut" and not last):
                                f.remove(g)
                    ne.append(cc)
                # Multicam Flow markers
                for mt, label in marks:
                    t = self.mc_tcstart + Fraction(mt).limit_denominator(1000000)
                    if lo <= t < hi:
                        kids = list(ne)
                        pos = max([i + 1 for i, k in enumerate(kids)
                                   if k.tag in ("mc-source",) + self.MARKER_TAGS or k.get("lane") is not None] or [0])
                        mk = ET.Element("marker", {"start": self.fstr(snap_local(t - st) + st), "duration": self.fstr(fd),
                                                   "value": label, "completed": "0"})
                        ne.insert(pos, mk)
                new_els.append(ne)
            for j, ne in enumerate(new_els):
                parent.insert(idx + j, ne)
            if total_removed > 0:
                # ripple: everything after this clip in the storyline slides up
                sibs = list(parent)
                for sib in sibs[idx + len(new_els):]:
                    if sib.get("offset") is not None:
                        sib.set("offset", self.fstr(ftime(sib.get("offset")) - total_removed))
                seq = seq_of.get(id(parent))
                if seq is not None and seq.get("duration"):
                    seq.set("duration", self.fstr(ftime(seq.get("duration")) - total_removed))
            stats["sections"] += 1
            stats["pieces"] += len(new_els)
        ET.indent(root)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n')
            f.write(ET.tostring(root, encoding="unicode"))
        return stats

GAP = "__GAP__"

# ----------------------------------------------------------------- analysis

AW = 480  # analysis width
THUMB_EVERY = 1.0  # seconds between review thumbnails
THUMB_W = 224
_face = None


def face_detector():
    global _face
    if _face is None:
        name = "haarcascade_frontalface_default.xml"
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        cands = [here]
        if getattr(cv2, "data", None) is not None:
            cands.append(os.path.join(cv2.data.haarcascades, name))
        for p in cands:
            if os.path.exists(p):
                c = cv2.CascadeClassifier(p)
                if not c.empty():
                    _face = c
                    break
        if _face is None:
            log("  note: face model not found, cutting without face detection")
            _face = False
    return _face


def run_ffmpeg(ffmpeg, src, ss, t, fps, w, h, hw, color=False):
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if hw:
        cmd += ["-hwaccel", "videotoolbox"]
    cmd += ["-ss", f"{ss:.4f}", "-t", f"{t:.4f}", "-i", src,
            "-vf", f"fps={fps},scale={w}:{h}:flags=area,format={'bgr24' if color else 'gray'}", "-an",
            "-f", "rawvideo", "pipe:1"]
    # errors go to a temp file: an unread stderr pipe fills up on damaged files and freezes ffmpeg
    import tempfile
    errfh = tempfile.TemporaryFile()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfh)
    p.errfh = errfh
    return p


def ffmpeg_errors(p):
    try:
        p.errfh.seek(0, 2)
        size = p.errfh.tell()
        p.errfh.seek(max(0, size - 4000))
        return p.errfh.read().decode(errors="ignore")
    except Exception:
        return ""
    finally:
        try:
            p.errfh.close()
        except Exception:
            pass


def analyze_angle(ffmpeg, angle, nbins, fps, t0, hw, use_vision=False):
    """Fill per-bin feature arrays for one angle."""
    f = {k: np.full(nbins, np.nan, dtype=np.float32)
         for k in ("sharp", "luma", "dx", "dy", "act", "diff", "faces", "facesize",
                   "aes", "util", "fq", "fsz", "vfaces", "humans", "hsz")}
    thumbs = {}
    det = face_detector()
    every = max(1, int(round(THUMB_EVERY * fps)))
    vis = None
    if use_vision:
        try:
            import vision_mac
            vis = vision_mac if vision_mac.available() else None
        except Exception:
            vis = None
    for c in angle["clips"]:
        if not c["video"]:
            continue
        if not c["src"] or not os.path.exists(c["src"]):
            log(f"  [{angle['name']}] missing media: {c['src']}")
            continue
        a, b = c["off"] - t0, c["off"] + c["dur"] - t0
        i0 = max(0, int(np.ceil(a * fps)))
        i1 = min(nbins, int(np.floor(b * fps)))
        if i1 <= i0:
            continue
        ss = c["src_in"] + (i0 / fps + t0 - c["off"])
        h = int(round(AW * c["h"] / c["w"] / 2) * 2)
        n = i1 - i0
        color = vis is not None
        fsize = AW * h * (3 if color else 1)
        for attempt_hw in ([hw, False] if hw else [False]):
            p = run_ffmpeg(ffmpeg, c["src"], ss, n / fps + 0.5 / fps, fps, AW, h, attempt_hw, color)
            prev = None
            hwin = None
            k = 0
            while k < n:
                buf = p.stdout.read(fsize)
                if len(buf) < fsize:
                    break
                if color:
                    bgr = np.frombuffer(buf, np.uint8).reshape(h, AW, 3)
                    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                else:
                    g = np.frombuffer(buf, np.uint8).reshape(h, AW)
                i = i0 + k
                gf = g.astype(np.float32)
                f["sharp"][i] = cv2.Laplacian(g, cv2.CV_32F).var()
                f["luma"][i] = gf.mean() / 255.0
                if prev is not None:
                    if hwin is None or hwin.shape != gf.shape:
                        hwin = cv2.createHanningWindow((AW, h), cv2.CV_32F)
                    (dx, dy), _ = cv2.phaseCorrelate(prev, gf, hwin)
                    inner = (slice(8, -8), slice(8, -8))
                    e0 = np.abs(gf - prev)[inner].mean()
                    M = np.float32([[1, 0, dx], [0, 1, dy]])
                    warped = cv2.warpAffine(prev, M, (AW, h), borderMode=cv2.BORDER_REPLICATE)
                    e1 = np.abs(gf - warped)[inner].mean()
                    # Repeating patterns (pews, chairs, lights) fool the tracker.
                    # Keep a shift only if it actually lines the frames up better.
                    if np.hypot(dx, dy) > 1.0 and e1 >= e0 * 0.97:
                        dx = dy = np.nan  # tracker unsure: unknown, not "still"
                        e1 = e0
                    f["dx"][i], f["dy"][i] = dx, dy
                    f["act"][i] = min(e0, e1) / 255.0
                    f["diff"][i] = e0 / 255.0
                else:
                    f["dx"][i] = f["dy"][i] = np.nan
                    f["act"][i] = np.nan
                faces = det.detectMultiScale(g, 1.15, 5, minSize=(18, 18)) if det else ()
                f["faces"][i] = len(faces)
                f["facesize"][i] = max((fw for (_, _, fw, _) in faces), default=0) / AW
                if color and i % every == 0:
                    ok_, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    vr = vis.analyze(jpg.tobytes()) if ok_ else {}
                    sl = slice(i, min(nbins, i + every))
                    for key, dst in (("aes", "aes"), ("fq", "fq"), ("fsz", "fsz"), ("nfaces", "vfaces"),
                                     ("humans", "humans"), ("hsz", "hsz")):
                        if key in vr:
                            f[dst][sl] = vr[key]
                    if "util" in vr:
                        f["util"][sl] = 1.0 if vr["util"] else 0.0
                if i % every == 0:
                    thumbs[i] = cv2.resize(g, (THUMB_W, int(THUMB_W * h / AW) // 2 * 2), interpolation=cv2.INTER_AREA)
                prev = gf
                k += 1
                if k % 200 == 0:
                    print(f"@progress video {angle['name']} {k} {n}", file=sys.stderr, flush=True)
            p.stdout.close()
            err = ffmpeg_errors(p)
            p.wait()
            if k > 0 or not attempt_hw:
                if k == 0:
                    log(f"  [{angle['name']}] could not decode {c['src']}: {err.strip()[:200]}")
                break
            log(f"  [{angle['name']}] hardware decode failed, retrying in software")
        log(f"  [{angle['name']}] analyzed {os.path.basename(c['src'])} ({ts(n / fps)})")
    return f, thumbs


def load_audio(ffmpeg, angle, t0, t1, rate=16000):
    """Mono float audio for the angle, laid out on the multicam timeline [t0, t1)."""
    n = int((t1 - t0) * rate)
    out = np.zeros(n, np.float32)
    have = np.zeros(n, bool)
    for c in angle["clips"]:
        if not c["src"] or not os.path.exists(c["src"]):
            log(f"  [{angle['name']}] missing audio: {c['src']}")
            continue
        a = max(t0, c["off"])
        b = min(t1, c["off"] + c["dur"])
        if b <= a:
            continue
        ss = c["src_in"] + (a - c["off"])
        cmd = [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{ss:.4f}", "-t", f"{b - a:.4f}",
               "-i", c["src"], "-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "pipe:1"]
        x = np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, np.float32)
        i0 = int((a - t0) * rate)
        k = min(len(x), n - i0)
        out[i0:i0 + k] = x[:k]
        have[i0:i0 + k] = True
    return out, have


def loudness(x, have, nbins, fps, rate=16000):
    spb = rate // fps
    n = min(nbins, len(x) // spb)
    db = np.full(nbins, np.nan, np.float32)
    rms = np.sqrt((x[:n * spb].reshape(n, spb) ** 2).mean(axis=1) + 1e-10)
    ok = have[:n * spb].reshape(n, spb).mean(axis=1) > 0.5
    db[:n] = np.where(ok, 20 * np.log10(rms), np.nan)
    return db


def transcribe(x, t0, model_name, rate=16000):
    """Speech to text with timestamps (multicam time). Runs locally."""
    try:
        from faster_whisper import WhisperModel
    except Exception:
        log("  note: faster-whisper not installed, skipping transcript")
        return None
    log(f"Transcribing ceremony audio locally ({model_name}) ...")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    lang = "en" if model_name.endswith(".en") else None
    segs, _ = model.transcribe(x, language=lang, vad_filter=True, beam_size=5,
                               condition_on_previous_text=False)
    out = []
    total = len(x) / rate
    for sg in segs:
        print(f"@progress transcript all {int(sg.end)} {int(total)}", file=sys.stderr, flush=True)
        txt = sg.text.strip()
        if txt:
            out.append({"start": round(t0 + sg.start, 2), "end": round(t0 + sg.end, 2), "text": txt})
    log(f"  transcript: {len(out)} lines")
    return out


def find_moments(db, feats, fps, main_idx, cfg):
    """Laughter / applause / cheering = sustained jump in loudness over the room's
    recent level. Big reactions = sudden on-screen activity on a face angle."""
    n = len(db)
    hits = np.zeros(n, bool)
    kinds = np.full(n, "", dtype=object)
    if np.isfinite(db).any():
        base = rolling(db, 20 * fps, np.nanmedian)
        ex = rolling(db - base, max(1, fps // 2), np.nanmean)
        loud = np.nan_to_num(ex) > cfg.moment_db
        hits |= loud
        kinds[loud] = "audio"
    for ai, f in enumerate(feats):
        act = f["act"]
        ref = rolling(act, 20 * fps, np.nanmedian)
        spike = np.nan_to_num(act / (ref + 1e-4)) > 2.5
        faces = np.nan_to_num(rolling(f["faces"], fps, np.nanmax)) > 0
        v = spike & faces & (ai != main_idx)
        kinds[v & ~hits] = "visual"
        hits |= v
    # group into moments, drop blips, merge close ones
    out, i = [], 0
    min_len = max(1, int(1.0 * fps))
    while i < n:
        if not hits[i]:
            i += 1
            continue
        j = i
        while j < n and hits[j]:
            j += 1
        if j - i >= min_len:
            kind = "audio" if (kinds[i:j] == "audio").any() else "visual"
            if out and i - out[-1][1] <= 2 * fps:
                out[-1][1] = j
                if kind == "audio":
                    out[-1][2] = "audio"
            else:
                out.append([i, j, kind])
        i = j
    return out


def motion_area(thumbs, nbins, fps):
    """Share of the frame that changed in the last second, after lining up camera
    motion. Spikes when someone walks into a static shot. Per-bin (held)."""
    out = np.full(nbins, np.nan, np.float32)
    keys = sorted(k for k in thumbs if k < nbins)
    prev = pk = None
    for k in keys:
        g = cv2.GaussianBlur(thumbs[k].astype(np.float32), (5, 5), 0)
        if prev is not None and k - pk <= 2 * fps and prev.shape == g.shape:
            (dx, dy), _ = cv2.phaseCorrelate(prev, g)
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            w = cv2.warpAffine(prev, M, (g.shape[1], g.shape[0]), borderMode=cv2.BORDER_REPLICATE)
            m = 8
            out[pk:k + 1] = float((np.abs(g - w)[m:-m, m:-m] > 22).mean())
        prev, pk = g, k
    return out

CUE_WORDS = {
    "kiss": ("kiss the bride", "you may now kiss", "you may kiss", "seal it with a kiss", "kiss your bride"),
    "announce": ("mr. and mrs", "mr and mrs", "husband and wife", "pronounce you", "introduce to you",
                 "present to you"),
    "ido": ("i do", "i will"),
    "rings": ("exchange the rings", "exchange of rings", "with this ring", "give you this ring"),
}


def ceremony_cues(transcript):
    """Find the beats every ceremony shares, from the words spoken."""
    out = []
    for sg in transcript or []:
        t = sg["text"].lower()
        for kind, words in CUE_WORDS.items():
            if any(w in t for w in words):
                if kind == "ido":
                    tt = t.strip(" .!,")
                    # the answer itself ("I do."), or a line that ENDS with it
                    # ("...as long as you both shall live. I do") - not "say I do".
                    ends = re.search(r"(live|life|together)[.,!]?\s+i do$", tt) is not None
                    if len(tt) > 12 and not ends:
                        continue
                    if ends:
                        st_ = max(sg["start"], sg["end"] - 2.5)
                        out.append((kind, st_, sg["end"], "I do"))
                        continue
                out.append((kind, sg["start"], sg["end"], sg["text"]))
    return out


def blocked_frames(thumbs, nbins, fps, hist=30, gap=4):
    """Someone in front of the lens: a big region (12%+) that doesn't match what this
    camera normally sees AND is softer than the background behind it (close to the
    lens = out of focus). Returns per-bin (area, softness ratio)."""
    keys = sorted(k for k in thumbs if k < nbins)
    area = np.zeros(nbins, np.float32)
    rel = np.full(nbins, 99.0, np.float32)
    if len(keys) < 12:
        return area, rel
    raw = [thumbs[k].astype(np.float32) for k in keys]
    sm = [cv2.GaussianBlur(f, (7, 7), 0) for f in raw]
    step = max(1, keys[1] - keys[0]) if len(keys) > 1 else fps
    for i, k in enumerate(keys):
        idx = list(range(max(0, i - hist - gap), max(0, i - gap))) + \
              list(range(min(len(keys), i + gap + 1), min(len(keys), i + gap + 1 + hist)))
        if len(idx) < 10:
            continue
        bg = np.median(np.stack([sm[j] for j in idx[::2]]), axis=0)
        if bg.shape != sm[i].shape:
            continue
        mask = (np.abs(sm[i] - bg) > 28).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
        if n <= 1:
            continue
        j = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
        a = st[j, cv2.CC_STAT_AREA] / mask.size
        blob = lab == j
        r = float(np.abs(cv2.Laplacian(raw[i], cv2.CV_32F))[blob].mean()) / \
            (float(np.abs(cv2.Laplacian(bg, cv2.CV_32F))[blob].mean()) + 1e-3)
        area[k:min(nbins, k + step)] = a
        rel[k:min(nbins, k + step)] = r
    return area, rel


def rolling(x, w, fn):
    """Centered rolling function that ignores NaN."""
    n = len(x)
    if n == 0:
        return x
    w = max(1, int(w))
    pad = w // 2
    xp = np.pad(x, (pad, w - 1 - pad), constant_values=np.nan)
    win = np.lib.stride_tricks.sliding_window_view(xp, w)
    with np.errstate(all="ignore"), __import__("warnings").catch_warnings():
        __import__("warnings").simplefilter("ignore")
        return fn(win, axis=1).astype(np.float32)


def score_angle(f, fps, is_main, cfg, roam_hint=False):
    """Return (score per bin, usable mask, reason codes).
    Philosophy: judge the SHOT, not the camera. Smooth moves (follows, pushes, gimbal
    work) are fine on any camera. Only whips, jitter, truly soft focus and black frames
    are rejected. What happens IN the frame (people moving, faces) earns the cut."""
    n = len(f["sharp"])
    covered = ~np.isnan(f["sharp"])
    # Focus vs this camera's sharpest frames in the last ~12s (short window, so a
    # gimbal changing framing isn't compared to a different shot from a minute ago).
    ref = rolling(f["sharp"], 12 * fps, lambda w, axis: np.nanpercentile(w, 85, axis=axis))
    ratio = np.where(ref > 0, f["sharp"] / ref, 0)
    ratio_s = rolling(ratio, max(1, fps), np.nanmedian)
    # Movement in frame-widths per second.
    vx = f["dx"] * fps / AW
    vy = f["dy"] * fps / AW
    win = max(2, fps)
    pan = np.hypot(rolling(vx, win, np.nanmean), rolling(vy, win, np.nanmean))
    shake = np.hypot(rolling(vx, win, np.nanstd), rolling(vy, win, np.nanstd))
    luma = f["luma"]
    act = f["act"]
    faces = rolling(f["faces"], win, np.nanmax)
    fsize = rolling(f["facesize"], win, np.nanmax)

    cov_pan = pan[covered & np.isfinite(pan)]
    roaming = roam_hint or (cov_pan.size > 0 and float((cov_pan > cfg.pan_max).mean()) > cfg.roam_frac)
    shake_lim = cfg.shake_max * (1.3 if roaming else 1.0)

    # Smooth move = jitter small compared to the move itself. Those are kept.
    smooth = np.nan_to_num(shake) <= np.maximum(0.5 * shake_lim, 0.45 * np.nan_to_num(pan))
    whip = np.nan_to_num(pan) > cfg.whip_max
    jerky_move = (np.nan_to_num(pan) > cfg.pan_max) & ~smooth
    diff = f.get("diff") if isinstance(f, dict) and "diff" in f else np.full(n, np.nan, np.float32)
    diff_ref = rolling(diff, 20 * fps, np.nanmedian)
    big = np.nan_to_num(diff / (diff_ref + 1e-4)) > 2.5
    lost_raw = np.isnan(f["dx"]) & big
    lost = rolling(np.where(covered, lost_raw.astype(np.float32), np.nan), win, np.nanmean) > 0.5
    moving = (whip | jerky_move | lost) & covered
    shaky = (shake > shake_lim) & ~smooth
    soft = np.nan_to_num(ratio_s) < cfg.focus_min
    dark = (luma < 0.05) | (luma > 0.97)
    blocked = np.zeros(n, bool)
    if isinstance(f, dict) and f.get("block_area") is not None:
        ba, br = f["block_area"], f["block_rel"]
        # Only trust the "normal view" when the camera has held its framing for the
        # surrounding ~30s (side cameras). A reframing gimbal would look "blocked".
        steady_cam = rolling(np.nan_to_num(pan).astype(np.float32), 30 * fps,
                             lambda w, axis: np.nanpercentile(w, 90, axis=axis)) < 0.05
        raw_b = steady_cam & (((ba >= cfg.block_area) & (br < 2.0)) | (ba >= 0.30))
        # a blocker is there for a moment; pad a little either side so we cut away cleanly
        blocked = np.nan_to_num(rolling(raw_b.astype(np.float32), fps, np.nanmax)) > 0
    usable = covered & ~soft & ~moving & ~shaky & ~dark & ~blocked
    # "Stable-ish": good enough for a gimbal at a big moment (exit, entrance).
    relaxed = covered & ~dark & ~blocked & ~whip & (np.nan_to_num(ratio_s) >= 0.22) & \
        (np.nan_to_num(shake) <= 1.7 * shake_lim)

    # Action in frame: subject motion (after removing camera motion) above this
    # camera's normal level for the last minute. People walking, hugging, cheering.
    act_base = rolling(act, 60 * fps, np.nanmedian)
    act_rel = np.nan_to_num(act / (act_base + 1e-4))
    action = np.zeros(n, np.float32)
    ma = f.get("marea") if isinstance(f, dict) else None
    if ma is not None:
        # Someone entering / moving through a static shot: changed area well above
        # this camera's normal for the last minute. Ignored while the camera moves.
        ma_base = rolling(ma, 60 * fps, np.nanmedian)
        jump = np.nan_to_num(ma) - np.nan_to_num(ma_base)
        steady = np.nan_to_num(pan) < cfg.pan_max
        action = np.clip((jump - 0.04) / 0.20, 0, 1) * steady * (np.nan_to_num(ma) < 0.30)
        action = np.nan_to_num(rolling(action.astype(np.float32), 2 * fps, np.nanmax))

    s = np.ones(n, np.float32)
    s += cfg.main_bonus if is_main else 0
    have_vis = isinstance(f, dict) and "fq" in f and np.isfinite(f["fq"]).any()
    if have_vis:
        # Apple Vision: how good the frame looks (composition, light, blur) and how
        # good the best face is (sharp, lit, expression), plus people present.
        aes = rolling(f["aes"], 2 * fps, np.nanmean)
        s += cfg.aes_weight * np.clip(np.nan_to_num(aes), -1, 1)
        s -= 0.6 * np.nan_to_num(rolling(f["util"], 2 * fps, np.nanmean))
        fq = np.nan_to_num(rolling(f["fq"], 2 * fps, np.nanmax))
        fsz = np.nan_to_num(rolling(f["fsz"], 2 * fps, np.nanmax))
        s += 0.9 * fq * np.clip(fsz / 0.06, 0.3, 1)
        s += 0.4 * np.clip(fsz / 0.12, 0, 1)
        s += 0.2 * np.clip(np.nan_to_num(f["humans"]), 0, 3) / 3
        faces = np.maximum(np.nan_to_num(faces), np.nan_to_num(f["vfaces"]))
        fsize = np.maximum(np.nan_to_num(fsize), fsz)
    else:
        s += 0.35 * np.clip(np.nan_to_num(faces), 0, 3) / 3
        s += 0.6 * np.clip(np.nan_to_num(fsize) / 0.12, 0, 1)
    s += cfg.action_bonus * action
    # A smooth camera move (gimbal sweep, slider, follow) earns the cut. A push-in or
    # pull-out barely translates the frame but changes all of it, smoothly.
    pn = np.nan_to_num(pan)
    sweep = smooth & (pn > 0.02) & ~whip & covered
    diff_rel = np.nan_to_num(diff / (diff_ref + 1e-4))
    push = smooth & (pn < 0.03) & (diff_rel > 1.6) & (diff_rel < 5) & ~lost & covered & \
        (np.nan_to_num(shake) < 0.5 * shake_lim)
    push = np.nan_to_num(rolling(push.astype(np.float32), 2 * fps, np.nanmean)) > 0.6
    move = np.clip(pn / 0.10, 0, 1) * sweep
    move = np.maximum(move, push.astype(np.float32) * 0.8)
    s += getattr(cfg, "move_bonus", 0.5) * move
    s -= 0.6 * np.clip(np.nan_to_num(shake) / shake_lim, 0, 1)
    s -= 0.6 * np.clip(1 - np.nan_to_num(ratio_s), 0, 1)
    s = np.where(usable, s, s - 6)
    s = np.where(covered, s, -50)

    reason = np.full(n, "", dtype=object)
    reason[~covered] = "no footage"
    reason[covered & dark] = "too dark/bright"
    reason[covered & soft] = "soft focus"
    reason[covered & moving] = "whip/jerky move"
    reason[covered & shaky] = "shaky"
    reason[covered & blocked] = "blocked"
    detail = {"relaxed": relaxed, "vision": bool(isinstance(f, dict) and "fq" in f and np.isfinite(f["fq"]).any()), "roaming": roaming, "focus": ratio_s, "pan": pan, "shake": shake, "faces": faces,
              "facesize": fsize, "act": act_rel, "action": action, "move": move}
    return s, usable, reason, detail

# ----------------------------------------------------------------- selection

def viterbi(S, switch):
    """S: (A, n) scores. Best angle per bin; switch = penalty per cut (scalar or per bin)."""
    A, n = S.shape
    sw = np.broadcast_to(np.asarray(switch, np.float32), (n,))
    dp = S[:, 0].copy()
    back = np.zeros((A, n), np.int16)
    for i in range(1, n):
        best = dp.argmax()
        jump = dp[best] - sw[i]
        choose_stay = dp >= jump
        back[:, i] = np.where(choose_stay, np.arange(A), best)
        dp = np.where(choose_stay, dp, jump) + S[:, i]
    path = np.zeros(n, np.int16)
    path[-1] = dp.argmax()
    for i in range(n - 1, 0, -1):
        path[i - 1] = back[path[i], i]
    return path


def runs(path):
    out, s = [], 0
    for i in range(1, len(path) + 1):
        if i == len(path) or path[i] != path[s]:
            out.append([s, i, int(path[s])])
            s = i
    return out


def enforce_min(path, S, U, min_bins, locked=None):
    """Merge shots that are too short. min_bins may vary per bin: calm talking needs
    longer shots than a laugh or a walk-in."""
    n = len(path)
    mb = np.broadcast_to(np.asarray(min_bins), (n,))
    changed = True
    while changed:
        changed = False
        r = runs(path)
        for k, (a, b, ang) in enumerate(r):
            need = int(mb[a:b].min())
            if b - a >= need or len(r) == 1:
                continue
            if locked is not None and locked[a:b].any():
                continue
            cands = []
            if k > 0:
                cands.append(r[k - 1][2])
            if k + 1 < len(r):
                cands.append(r[k + 1][2])
            best = max(cands, key=lambda c: (U[c, a:b].mean(), S[c, a:b].mean()))
            path[a:b] = best
            changed = True
            break
    return path


def earn_cuts(path, S, U, fps, margin=0.3, max_len_s=12.0, locked=None, sandwich_only=False, max_hold=None,
              soft=None):
    """Don't cut for the sake of cutting. A short excursion to another angle stays only
    if that angle is clearly better there; otherwise the neighbouring shot keeps
    rolling (e.g. a soft camera catching someone walking across, while the wide is clean)."""
    n = len(path)
    changed, merged = True, 0
    while changed:
        changed = False
        r = runs(path)
        for k, (a, b, ang) in enumerate(r):
            if b - a > max_len_s * fps or len(r) == 1:
                continue
            if locked is not None and locked[a:b].any():
                continue
            cands = set()
            if k > 0:
                cands.add(r[k - 1][2])
            if k + 1 < len(r):
                cands.add(r[k + 1][2])
            if sandwich_only and not (0 < k < len(r) - 1 and r[k - 1][2] == r[k + 1][2]):
                continue
            here = S[ang, a:b].mean()
            best = None
            for c in cands:
                if U[c, a:b].mean() < 0.95:
                    continue
                blurry = soft is not None and soft[ang, a:b].mean() > 0.5
                if max_hold is not None and not blurry:
                    # merging must not create a hold longer than the pacing allows
                    lo = r[k - 1][0] if k > 0 and r[k - 1][2] == c else a
                    hi = r[k + 1][1] if k + 1 < len(r) and r[k + 1][2] == c else b
                    if hi - lo > int(np.median(np.broadcast_to(np.asarray(max_hold), (n,))[lo:hi])):
                        continue
                alt = S[c, a:b].mean()
                if here < alt + margin and (best is None or alt > best[0]):
                    best = (alt, c)
            if best:
                path[a:b] = best[1]
                merged += 1
                changed = True
                break
    return path, merged


def add_cutaways(path, S, U, main, min_bins, max_bins, drop, fps=4, prefer=(), locked=None,
                 fsz=None, boredom=0.6, variety=0.4):
    """Break up long shots with the best usable alternate angle. Calm talking holds
    longer (max_bins per bin) and gets short cutaways; preferred angles (gimbal,
    reaction cameras) get first pick."""
    A = S.shape[0]
    n = len(path)
    mxb = np.broadcast_to(np.asarray(max_bins), (n,))
    mnb = np.broadcast_to(np.asarray(min_bins), (n,))
    # a cutaway has to be one of that camera's decent shots, not its worst
    floor = [float(np.percentile(S[c][U[c]], 25)) if U[c].any() else 0.0 for c in range(A)]
    for _ in range(5000):
        r = runs(path)
        done = True
        for a, b, ang in r:
            limit = int(np.median(mxb[a:b]))
            if b - a <= limit:
                continue
            if locked is not None and locked[a:b].mean() > 0.5:
                continue
            need = int(np.median(mnb[a:b]))
            L = int(np.clip(limit * 0.3, max(2 * fps, need), max(6 * fps, need)))
            if (b - a) - L < 2 * need:
                continue
            best = None
            held_all = (b - a) / max(limit, 1)
            for c in range(A):
                if c == ang:
                    continue
                for st in range(a + need, b - need - L + 1, max(1, fps // 2)):
                    um = U[c, st:st + L].mean()
                    if um < 1.0 and not (held_all >= 2.0 and um >= 0.9):
                        continue
                    if locked is not None and locked[st:st + L].any():
                        continue
                    sc = S[c, st:st + L].mean()
                    if sc < floor[c] - 0.3 and held_all < 3.0:   # way too long: any usable shot will do
                        continue
                    gain = sc - S[ang, st:st + L].mean()
                    centered = -abs((st + L / 2) - (a + b) / 2) / (b - a)
                    # variety: a wide after a tight (or the other way) is worth more
                    vb = 0.0
                    if fsz is not None:
                        f1 = float(np.nanmedian(fsz[ang][a:b])) if np.isfinite(fsz[ang][a:b]).any() else 0.0
                        f2 = float(np.nanmedian(fsz[c][st:st + L])) if np.isfinite(fsz[c][st:st + L]).any() else 0.0
                        if max(f1, f2) > 0.01 and max(f1, f2) >= 1.8 * max(min(f1, f2), 1e-3):
                            vb = variety
                    val = gain + 0.3 * centered + (0.5 if c in prefer else 0.0) + vb
                    # boredom: the longer we've held, the less better the next shot has to be
                    held = (b - a) / max(limit, 1)
                    allow = drop + boredom * min(max(held - 1.0, 0.0), 4.0) + vb
                    if held >= 3.0:   # way too long: any decent shot of another camera will do
                        allow = 1e9
                    if gain >= -allow - (0.6 if c in prefer else 0.0) and (best is None or val > best[0]):
                        best = (val, c, st)
            if best:
                _, c, st = best
                path[st:st + L] = c
                done = False
                break
            if os.environ.get("AUTOCUT_DEBUG") and (b - a) > 3 * limit:
                for c in range(A):
                    if c == ang:
                        continue
                    seg = slice(a + need, b - need)
                    log(f"    long hold {a / fps:.0f}-{b / fps:.0f}s: angle {c} usable {U[c, seg].mean():.2f}, "
                        f"score {S[c, seg].mean():.2f} vs {S[ang, seg].mean():.2f}, floor {floor[c]:.2f}, "
                        f"L={L}, locked {0 if locked is None else locked[seg].mean():.2f}")
        if done:
            break
    return path


def gimbal_sprinkle(path, S, U, gims, fps, every_s, locked=None, min_s=3.0, max_s=7.0, drop=1.2, min_bins=None):
    """Make sure the gimbal / guest camera shows up regularly: in any stretch of
    every_s seconds without it, cut in its best usable few seconds."""
    if not gims:
        return path
    n = len(path)
    W = int(every_s * fps)
    Lmin, Lmax = int(min_s * fps), int(max_s * fps)
    i = 0
    while i < n - W:
        win = slice(i, i + W)
        if np.isin(path[win], list(gims)).any():
            # restart the window after the last gimbal shot in it
            last = i + int(np.where(np.isin(path[win], list(gims)))[0].max()) + 1
            i = max(i + fps, last)
            continue
        best = None
        for g in gims:
            u = U[g, win] & (locked[win] == 0 if locked is not None else True)
            j = 0
            while j < W:
                if not u[j]:
                    j += 1
                    continue
                k = j
                while k < W and u[k] and k - j < Lmax:
                    k += 1
                need = Lmin if min_bins is None else max(Lmin, int(np.asarray(min_bins)[i + j:i + k].min()))
                if k - j >= need:
                    a, b = i + j, i + k
                    gain = S[g, a:b].mean() - S[path[a:b], np.arange(a, b)].mean()
                    if gain >= -drop and (best is None or gain > best[0]):
                        best = (gain, g, a, b)
                j = k + 1
        if best:
            _, g, a, b = best
            path[a:b] = g
            i = b
        else:
            if os.environ.get("AUTOCUT_DEBUG"):
                log(f"    sprinkle: no gimbal option in bins {i}-{i + W}")
            i += W // 2
    return path

def clean_cuts(path, feats, U, fps, locked=None, look_s=2.0, min_keep_s=1.0):
    """Cut BEFORE a camera starts repositioning, not after.
    For each cut away from a camera, rewind up to look_s seconds to where its motion
    first departed from how it was moving during the shot (speed-up, direction change,
    drifting off the subject) and cut one sample before that. For each cut INTO a
    camera that is mid-reposition, wait until it settles (if it settles quickly)."""
    n = len(path)
    A = len(feats)
    vx = [np.asarray(f["dx"], np.float32) * fps / AW for f in feats]
    vy = [np.asarray(f["dy"], np.float32) * fps / AW for f in feats]
    look = int(look_s * fps)
    keep = int(min_keep_s * fps)
    moved = {"earlier": 0, "later": 0, "seconds": 0.0}

    def deviation(c, a, b):
        """How different each sample's motion is from the shot's typical motion."""
        x, y = vx[c][a:b], vy[c][a:b]
        if np.isfinite(x).sum() < 3:
            return None, None
        mx, my = np.nanmedian(x), np.nanmedian(y)
        dev = np.hypot(np.nan_to_num(x - mx, nan=np.nan), np.nan_to_num(y - my, nan=np.nan))
        base = np.nanmedian(dev)
        thr = max(0.05, 2.5 * (base if np.isfinite(base) else 0))
        return dev, thr

    r = runs(path)
    for k in range(len(r) - 1):
        a, b, ang = r[k]
        nxt = r[k + 1][2]
        # --- leaving: did this camera start a move before we cut?
        dev, thr = deviation(ang, a, b)
        if dev is not None:
            t = b - a - 1  # index within the shot
            lo = max(keep, t - look)
            if t > 0 and np.nan_to_num(dev[t], nan=thr + 1) > thr:
                while t - 1 >= lo and np.nan_to_num(dev[t - 1], nan=thr + 1) > thr:
                    t -= 1
                new_b = a + max(lo, t - 1)  # one sample of margin before the move
                if new_b < b and U[nxt, new_b:b].all() and (locked is None or not locked[new_b:b].any()):
                    path[new_b:b] = nxt
                    moved["earlier"] += 1
                    moved["seconds"] += (b - new_b) / fps
        # --- entering: is the next camera mid-reposition right at the cut?
        r2 = runs(path)
        cut = next((x for x in r2 if x[0] <= b < x[1] and x[2] == nxt), None)
        if cut is None:
            continue
        na, nb, _ = cut
        dev2, thr2 = deviation(nxt, na, nb)
        if dev2 is None or na - 0 < 1:
            continue
        if np.nan_to_num(dev2[0], nan=0) > thr2:
            j = 0
            while j < min(len(dev2), int(1.5 * fps)) and np.nan_to_num(dev2[j], nan=thr2 + 1) > thr2:
                j += 1
            settle = na + j + 1
            prev = path[na - 1]
            if j < int(1.5 * fps) and settle < nb - keep and U[prev, na:settle].all() and \
                    (locked is None or not locked[na:settle].any()):
                path[na:settle] = prev
                moved["later"] += 1
    return path, moved


# ----------------------------------------------------------------- review sheets

def make_sheets(thumbs, path, angles, fps, nbins, outdir, t0, reasons=None, per_sheet=60.0, cols=10):
    """One image per minute: a row per camera, a column every 6 seconds.
    Gold box = angle the cut uses. Red label = why an angle is unusable there."""
    os.makedirs(outdir, exist_ok=True)
    step = max(1, int(round(per_sheet / cols * fps)))
    th = next((t.shape[0] for d in thumbs for t in d.values()), 126)
    tw = next((t.shape[1] for d in thumbs for t in d.values()), THUMB_W)
    every = max(1, int(round(THUMB_EVERY * fps)))
    files = []
    for s0 in range(0, nbins, cols * step):
        rows = []
        for ai, d in enumerate(thumbs):
            cells = []
            for j in range(cols):
                i = s0 + j * step
                key = (i // every) * every
                im = d.get(key)
                im = cv2.cvtColor(im if im is not None else np.zeros((th, tw), np.uint8), cv2.COLOR_GRAY2BGR)
                if im.shape[:2] != (th, tw):
                    im = cv2.resize(im, (tw, th))
                if reasons is not None and i < nbins and reasons[ai][i]:
                    cv2.putText(im, reasons[ai][i].upper(), (4, th - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 3)
                    cv2.putText(im, reasons[ai][i].upper(), (4, th - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (60, 60, 255), 1)
                if path is not None and i < nbins and path[i] == ai:
                    cv2.rectangle(im, (0, 0), (tw - 1, th - 1), (0, 200, 255), 4)
                cells.append(im)
            row = np.hstack(cells)
            label = np.full((th, 90, 3), 30, np.uint8)
            cv2.putText(label, angles[ai]["name"][:9], (5, th // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            rows.append(np.hstack([label, row]))
        hdr = np.full((22, rows[0].shape[1], 3), 15, np.uint8)
        for j in range(cols):
            cv2.putText(hdr, ts(t0 + (s0 + j * step) / fps), (90 + j * tw + 4, 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 220, 220), 1)
        sheet = np.vstack([hdr] + rows)
        fn = os.path.join(outdir, f"sheet_{ts(t0 + s0 / fps).replace(':', '-')}.jpg")
        cv2.imwrite(fn, sheet, [cv2.IMWRITE_JPEG_QUALITY, 82])
        files.append(fn)
    return files


def write_brief(path, proj, VA, main_ang, results, fps, inplace):
    """Plain-text brief of the analysis (written with --sheets)."""
    L = [f"# Multicam Flow brief: {proj.media.get('name')}"]
    L.append(f"Mode: {'recut in place (project: ' + ', '.join(proj.project_names) + ')' if inplace else 'new project'}")
    L.append("All times are multicam time; transcript, sheets and brain.json use this clock.")
    L.append(f"Video angles: {', '.join(a['name'] for a in VA)} | preferred: {main_ang['name'] if main_ang else 'none'}")
    roam = sorted({n for r in results for n in r["roam"]})
    if roam:
        L.append(f"Roaming (gimbal/handheld) cameras: {', '.join(roam)}. Smooth movement there is intentional.")
    for si, r in enumerate(results, 1):
        t0, U, R, D = r["t0"], r["U"], r["R"], r["D"]
        L.append("")
        L.append(f"## Section {si}: {ts(r['t0'])}-{ts(r['t1'])} ({ts(r['t1'] - r['t0'])}), audio {r['audio']['name']}")
        L.append("Camera availability every 15s (OK, or why not usable; f=max faces)")
        cr = r.get("cam_role") or {}
        if cr:
            L.append("Camera roles: " + ", ".join(f"{VA[i]['name']}={x}" for i, x in cr.items()))
        L.append("time     | " + " | ".join(f"{a['name']:<16}" for a in VA) + " | talking")
        step = int(15 * fps)
        for i in range(0, U.shape[1], step):
            cells = []
            for ai in range(len(VA)):
                sl = slice(i, min(i + step, U.shape[1]))
                ok = U[ai, sl].mean()
                rs = [x for x in R[ai][sl] if x]
                why = max(set(rs), key=rs.count) if rs else ""
                fc = np.nanmax(D[ai]["faces"][sl]) if np.isfinite(D[ai]["faces"][sl]).any() else 0
                cells.append(f"{('OK' if ok > 0.8 else (why or 'mixed'))[:11]:<11} f{int(fc)}{'':<2}")
            spk = [x for x in r.get("speaker", np.array([]))[i:i + step] if x]
            who = max(set(spk), key=spk.count) if len(spk) > step // 4 else ""
            L.append(f"{ts(t0 + i / fps):<8} | " + " | ".join(f"{c:<16}" for c in cells) + f" | {who}")
        L.append("Loudness spikes: " + (", ".join(f"{ts(t0 + a / fps)}-{ts(t0 + b / fps)}" for a, b, k in r["moments"]) or "none"))
        L.append("Transcript:")
        for sg in (r["transcript"] or []):
            L.append(f"[{ts(sg['start'])}] {sg['text']}")
        if not r["transcript"]:
            L.append("(none)")
    L.append("")
    L.append("## brain.json format (optional: write this next to the brief to lock or avoid shots)")
    L.append("""{
  "moments": [
    {"start": "14:02", "end": "14:20", "label": "Ring exchange", "angle": "Cam 1", "lock": false},
    {"start": "31:45", "end": "31:52", "label": "First kiss", "angle": "Cam 2", "lock": true}
  ],
  "avoid": [ {"start": "20:00", "end": "21:30", "angle": "Cam 3", "why": "guest blocking"} ],
  "notes": "free text summary"
}
angle may be omitted (marker only). lock=true forces the angle even if the scores disagree.""")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")



def zoom_sheet(thumbs, angles, fps, t0, a, b, path, out_png, step_s=1.0, reasons=None, big=False):
    """Dense review strip: every camera, one frame per second, for [a, b] (multicam sec)."""
    cols = []
    i = int((a - t0) * fps)
    end = int((b - t0) * fps)
    every = max(1, int(round(THUMB_EVERY * fps)))
    th = next((t.shape[0] for d in thumbs for t in d.values()), 126)
    tw = next((t.shape[1] for d in thumbs for t in d.values()), THUMB_W)
    rows = []
    for ai, d in enumerate(thumbs):
        cells = []
        j = i
        while j < end:
            key = (j // every) * every
            im = d.get(key)
            im = cv2.cvtColor(im if im is not None else np.zeros((th, tw), np.uint8), cv2.COLOR_GRAY2BGR)
            if im.shape[:2] != (th, tw):
                im = cv2.resize(im, (tw, th))
            if not big:
                im = cv2.resize(im, (tw * 2 // 3, th * 2 // 3))
            if reasons is not None and 0 <= j < len(reasons[ai]) and reasons[ai][j]:
                cv2.putText(im, reasons[ai][j][:10].upper(), (3, im.shape[0] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (60, 60, 255), 1)
            if path is not None and 0 <= j < len(path) and path[j] == ai:
                cv2.rectangle(im, (0, 0), (im.shape[1] - 1, im.shape[0] - 1), (0, 200, 255), 3)
            cells.append(im)
            j += int(step_s * fps)
        lab = np.full((cells[0].shape[0], 70, 3), 30, np.uint8)
        cv2.putText(lab, angles[ai]["name"][:8], (4, cells[0].shape[0] // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
        rows.append(np.hstack([lab] + cells))
    hdr = np.full((18, rows[0].shape[1], 3), 15, np.uint8)
    cw = rows[0].shape[1] - 70
    n = (end - i + int(step_s * fps) - 1) // int(step_s * fps)
    for k in range(0, n, 2):
        cv2.putText(hdr, ts(a + k * step_s)[:8], (70 + k * cw // n + 2, 13), cv2.FONT_HERSHEY_SIMPLEX, 0.33, (220, 220, 220), 1)
    cv2.imwrite(out_png, np.vstack([hdr] + rows), [cv2.IMWRITE_JPEG_QUALITY, 85])

# ----------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Auto-cut a Final Cut multicam by picture quality.")
    ap.add_argument("input", nargs="?", help=".fcpxml or .fcpxmld exported from Final Cut")
    ap.add_argument("--vision-check", action="store_true", help="test Apple Vision and write vision_check.json")
    ap.add_argument("--multicam", help="multicam clip name, if the export has several")
    ap.add_argument("--main", help="preferred angle, or 'none' to pick the best each moment")
    ap.add_argument("--audio", help="angle whose audio plays throughout, or 'timeline' to keep each section's audio")
    ap.add_argument("--fps", type=int, default=4, help="analysis samples per second")
    ap.add_argument("--min-shot", type=float, default=2.5)
    ap.add_argument("--max-shot", type=float, default=9.0)
    ap.add_argument("--switch", type=float, default=1.0, help="how hard it resists cutting (higher = fewer cuts)")
    ap.add_argument("--main-bonus", type=float, default=0.4)
    ap.add_argument("--whip-max", type=float, default=0.40, help="camera moves faster than this (frame widths/sec) are whips")
    ap.add_argument("--action-bonus", type=float, default=1.4, help="how much people moving in frame earns the cut")
    ap.add_argument("--music-bonus", type=float, default=1.2, help="edge for the moving camera during music sections")
    ap.add_argument("--music-min", type=float, default=40, help="seconds without speech that count as a music section")
    ap.add_argument("--block-area", type=float, default=0.12, help="share of frame a soft foreground blob must cover to count as blocked")
    ap.add_argument("--roles", default="", help='who each camera shows: "Cam 1=bride,Cam 2=groom,Cam 3=gimbal"')
    ap.add_argument("--speaker-bonus", type=float, default=1.0, help="pull toward the talking person's camera")
    ap.add_argument("--reaction-bonus", type=float, default=2.5, help="pull toward the couple on laughs/applause")
    ap.add_argument("--calm-min-shot", type=float, default=5.0, help="shortest shot while someone is just talking")
    ap.add_argument("--calm-max-shot", type=float, default=16.0, help="longest hold while someone is just talking")
    ap.add_argument("--calm-switch", type=float, default=3.0, help="how much harder it is to cut during calm talking")
    ap.add_argument("--gimbal-every", type=float, default=45.0, help="cut in the gimbal/guest camera at least this often (sec)")
    ap.add_argument("--aes-weight", type=float, default=0.8, help="how much Apple's frame-quality score matters")
    ap.add_argument("--no-vision", action="store_true", help="skip Apple Vision scoring")
    ap.add_argument("--zoom", action="append", default=[], help="dense review strip for a range, e.g. 44:20-44:58")
    ap.add_argument("--empty-penalty", type=float, default=1.5, help="penalty for shots with no people when others have people")
    ap.add_argument("--remove-gaps", action="store_true", help="cut out stretches where no camera has video")
    ap.add_argument("--keep-gaps", action="store_true", help="never cut out no-video gaps (overrides earlier runs)")
    ap.add_argument("--gap-min", type=float, default=1.0, help="shortest no-video stretch to cut out (sec)")
    ap.add_argument("--exit-bonus", type=float, default=2.0, help="gimbal priority after the announcement (grand exit)")
    ap.add_argument("--focus-min", type=float, default=0.35, help="min sharpness vs the angle's recent best")
    ap.add_argument("--pan-max", type=float, default=0.12, help="max camera movement, frame widths/sec")
    ap.add_argument("--pan-max-roam", type=float, default=0.35, help="movement limit for gimbal/roaming cameras")
    ap.add_argument("--roam-frac", type=float, default=0.25, help="share of time moving that marks a camera as roaming")
    ap.add_argument("--shake-max", type=float, default=0.10, help="max jitter, frame widths/sec")
    ap.add_argument("--cutaway-drop", type=float, default=0.5, help="how much worse a cutaway may score")
    ap.add_argument("--style", choices=["ceremony", "party"], default="ceremony",
                    help="ceremony: hold on the talker, cut to reactions. party: faster, favor the moving camera")
    ap.add_argument("--move-bonus", type=float, default=0.5, help="edge for smooth camera moves (gimbal sweeps, push-ins)")
    ap.add_argument("--earn-margin", type=float, default=0.3, help="how much better a short cut-in must be to stay")
    ap.add_argument("--boredom", type=float, default=0.6, help="how fast a long hold makes an equal shot worth cutting to")
    ap.add_argument("--variety-bonus", type=float, default=0.4, help="edge for a different shot size (wide vs tight)")
    ap.add_argument("--moment-db", type=float, default=9.0, help="loudness jump (dB) that counts as a moment")
    ap.add_argument("--moment-bonus", type=float, default=1.5, help="how strongly moments pull to a reaction angle")
    ap.add_argument("--no-moments", action="store_true")
    ap.add_argument("--new-project", action="store_true", help="make a fresh project instead of recutting yours in place")
    ap.add_argument("--full", action="store_true", help="cut the whole multicam, not just your timeline's range")
    ap.add_argument("--range", help="only cut this range, e.g. 05:00-20:00")
    ap.add_argument("--force", action="append", default=[], help='force angle: 12:30-12:41="Cam B"')
    ap.add_argument("--no-proxy", action="store_true", help="analyze originals even if proxies exist")
    ap.add_argument("--no-hw", action="store_true", help="disable hardware decoding")
    ap.add_argument("--sheets", action="store_true", help="write review contact sheets")
    ap.add_argument("--out", help="output folder (default: next to input)")
    ap.add_argument("--search", action="append", default=[], help="folder to look in if media paths moved")
    ap.add_argument("--reuse", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--fresh", action="store_true", help="redo the analysis even if cached")
    ap.add_argument("--list-angles", action="store_true", help="print angles and defaults as JSON, then exit")
    ap.add_argument("--no-transcript", action="store_true")
    ap.add_argument("--no-brain", action="store_true", help="ignore brain.json")
    ap.add_argument("--whisper", default="small.en", help="local speech model (tiny.en, base.en, small.en, medium.en)")
    # Defaults from settings.txt next to this script (key = value lines).
    sfile = os.path.join(os.environ.get("MCF_DATA", ""), "settings.txt")
    if not os.path.exists(sfile):
        sfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.txt")
    if os.path.exists(sfile):
        defaults = {}
        for line in open(sfile, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = [x.strip() for x in line.split("=", 1)]
                k = k.lower().replace(" ", "_").replace("-", "_")
                if not v:
                    continue
                act = next((a for a in ap._actions if a.dest == k), None)
                if act is not None and act.type in (int, float):
                    v = act.type(v)
                defaults[k] = v
        ap.set_defaults(**defaults)
    cfg = ap.parse_args()
    if cfg.keep_gaps:
        cfg.remove_gaps = False
    if cfg.vision_check:
        import vision_mac
        rep = vision_mac.self_test()
        out = os.path.join(os.environ.get("MCF_DATA") or os.path.dirname(os.path.abspath(__file__)), "vision_check.json")
        json.dump(rep, open(out, "w"), indent=1)
        print(json.dumps(rep))
        return

    t_start = time.time()
    Project.search_roots = cfg.search
    proj = Project(cfg.input, cfg.multicam, use_proxy=not cfg.no_proxy)
    base = os.path.splitext(os.path.basename(cfg.input.rstrip("/")))[0]
    outdir = cfg.out or os.path.join(os.path.dirname(os.path.abspath(cfg.input.rstrip("/"))), f"{base} Multicam Flow")
    log(f"Multicam: {proj.media.get('name')}  length {ts(proj.duration)}  angles: {[a['name'] for a in proj.angles]}")

    run_file = os.path.join(outdir, "run.json")
    if os.path.exists(run_file) and not cfg.list_angles:
        prev = json.load(open(run_file))
        cfg.main = cfg.main if "--main" in sys.argv else prev.get("main", cfg.main)
        cfg.audio = cfg.audio if "--audio" in sys.argv else prev.get("audio", cfg.audio)
        if "--remove-gaps" not in sys.argv and "--keep-gaps" not in sys.argv and prev.get("remove_gaps"):
            cfg.remove_gaps = True
        if "--roles" not in sys.argv and prev.get("roles"):
            cfg.roles = prev["roles"]
        if "--new-project" not in sys.argv and prev.get("new_project"):
            cfg.new_project = True
        if not cfg.range and "--full" not in sys.argv and prev.get("range"):
            cfg.range = prev["range"]
        if "--style" not in sys.argv and prev.get("style"):
            cfg.style = prev["style"]
    if cfg.style == "party":
        # Party: faster cuts, the moving camera leads. Speeches still hold (calm pacing).
        for flag, attr, val in (("--min-shot", "min_shot", 2.0), ("--max-shot", "max_shot", 6.0),
                                ("--gimbal-every", "gimbal_every", 20.0), ("--action-bonus", "action_bonus", 2.0),
                                ("--move-bonus", "move_bonus", 0.9), ("--music-bonus", "music_bonus", 1.6),
                                ("--switch", "switch", 0.7)):
            if flag not in sys.argv:
                setattr(cfg, attr, val)
    VA = proj.video_angles
    if not VA:
        sys.exit("No video angles found in this multicam.")
    A = len(VA)
    ex_cut = proj.existing
    inplace = bool(ex_cut) and not cfg.new_project and not cfg.range and not cfg.full

    no_main = str(cfg.main or "").strip().lower() in ("none", "no preference", "-")
    main_ang = proj.angle(cfg.main) if (cfg.main and not no_main) else None
    if main_ang is not None and not main_ang["has_video"]:
        main_ang = None
    if main_ang is None and ex_cut and not no_main:
        use = {}
        for st, du, v, a_ in ex_cut:
            if v:
                use[v] = use.get(v, 0) + du
        cand = proj.angle_by_id(max(use, key=use.get)) if use else None
        main_ang = cand if cand and cand["has_video"] else None
    if not no_main:
        main_ang = main_ang or VA[0]
    main_name = main_ang["name"] if main_ang else "none"

    # Audio: "timeline" keeps whatever audio each section already uses.
    keep_audio = str(cfg.audio or "").strip().lower() in ("timeline", "keep", "") and bool(ex_cut)
    audio_override = None if keep_audio else proj.angle(cfg.audio) if cfg.audio else None
    fallback_audio = audio_override or main_ang or VA[0]

    # Sections to cut (multicam time).
    if inplace:
        rs = sorted((st, st + du, a_) for st, du, v, a_ in ex_cut if du > 0)
        segs = []
        for a, b, aid in rs:
            if segs and a <= segs[-1][1] + 0.25:
                segs[-1][1] = max(segs[-1][1], b)
                segs[-1][2].append((b - a, aid))
            else:
                segs.append([a, b, [(b - a, aid)]])
        sections = []
        for a, b, auds in segs:
            tot = {}
            for d_, aid in auds:
                if aid:
                    tot[aid] = tot.get(aid, 0) + d_
            aang = audio_override or (proj.angle_by_id(max(tot, key=tot.get)) if tot else None) or fallback_audio
            sections.append((max(0.0, a), min(proj.duration, b), aang))
    else:
        t0, t1 = 0.0, proj.duration
        if ex_cut and not cfg.full:
            t0 = max(0.0, min(st for st, du, v, a_ in ex_cut))
            t1 = min(proj.duration, max(st + du for st, du, v, a_ in ex_cut))
        if cfg.range:
            a, b = cfg.range.split("-")
            t0, t1 = parse_clock(a), min(parse_clock(b), proj.duration)
        aang = audio_override or fallback_audio
        if keep_audio and ex_cut:
            aids = [a_ for st, du, v, a_ in ex_cut if a_]
            if aids:
                aang = proj.angle_by_id(max(set(aids), key=aids.count)) or aang
        sections = [(t0, t1, aang)]
    total_len = sum(b - a for a, b, _ in sections)

    if cfg.list_angles:
        auds = sorted({x[2]["name"] for x in sections})
        print(json.dumps({
            "multicam": proj.media.get("name"), "uid": proj.media.get("uid") or proj.media.get("id"),
            "angles": [{"name": x["name"], "video": x["has_video"]} for x in proj.angles],
            "main": main_name, "audio": "timeline" if keep_audio else sections[0][2]["name"],
            "timeline_audio": auds,
            "mode": "inplace" if inplace else "new",
            "projects": proj.project_names,
            "sections": [[round(a, 2), round(b, 2), x["name"]] for a, b, x in sections],
            "section_count": len(sections), "total_text": ts(total_len),
            "lav_roles": sorted(proj.role_audio), "roles": cfg.roles or "",
            "range_text": f"{ts(sections[0][0])}-{ts(sections[-1][1])}",
            "from_timeline": bool(ex_cut)}))
        return

    os.makedirs(outdir, exist_ok=True)
    global LOG_FH
    try:
        LOG_FH = open(os.path.join(outdir, "run.log"), "a", encoding="utf-8")
        LOG_FH.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(sys.argv[1:])}\n")
    except OSError:
        LOG_FH = None
    with open(run_file, "w") as fh:
        json.dump({"main": main_name, "audio": "timeline" if keep_audio else (audio_override or fallback_audio)["name"],
                   "new_project": bool(cfg.new_project), "roles": cfg.roles or "",
                   "remove_gaps": bool(cfg.remove_gaps), "style": cfg.style,
                   "range": cfg.range or ""}, fh)
    log(f"{'Recutting in place' if inplace else 'New project'}: {len(sections)} section(s), {ts(total_len)} total")

    brain_path = os.path.join(outdir, "brain.json")
    brain = json.load(open(brain_path, encoding="utf-8")) if os.path.exists(brain_path) and not cfg.no_brain else None
    if brain:
        log(f"Applying manual decisions from brain.json: {len(brain.get('moments', []))} moments, {len(brain.get('avoid', []))} avoids")
    fps = cfg.fps
    names = {a["name"].lower(): i for i, a in enumerate(VA)}
    main_idx = VA.index(main_ang) if main_ang else -1
    cam_role = {}
    for part in (cfg.roles or "").split(","):
        if "=" in part:
            nm, rl = part.split("=", 1)
            ai_ = names.get(nm.strip().lower())
            if ai_ is not None and rl.strip():
                cam_role[ai_] = rl.strip().lower()
    gims_cfg = {i for i, r in cam_role.items() if r in ("gimbal", "roaming", "guests")}
    face_cam = {r: [i for i, rr in cam_role.items() if rr == r] for r in ("bride", "groom", "officiant", "couple")}
    couple_cams = face_cam["bride"] + face_cam["groom"] + face_cam["couple"]
    if cam_role:
        log("Camera roles: " + ", ".join(f"{VA[i]['name']}={r}" for i, r in cam_role.items()))
    if proj.role_audio:
        log("Lav mics found: " + ", ".join(sorted(proj.role_audio)))
    review_dir = os.path.join(outdir, "review")
    os.makedirs(review_dir, exist_ok=True)
    for old in os.listdir(review_dir):
        if old.startswith("sheet_"):
            try:
                os.remove(os.path.join(review_dir, old))
            except OSError:
                pass  # can't delete here; the new sheets overwrite them
    ff = None
    hw = sys.platform == "darwin" and not cfg.no_hw
    results = []
    any_fresh = False

    for k, (t0, t1, aang) in enumerate(sections):
        nbins = int((t1 - t0) * fps)
        if nbins < fps:
            continue
        tag = f"{t0:.2f}_{t1:.2f}_{fps}"
        vcache = os.path.join(outdir, f"video_{tag}.npz")
        acache = os.path.join(outdir, f"audio_{tag}_{re.sub('[^A-Za-z0-9]', '', aang['id'])}.npz")
        tpath = os.path.join(outdir, f"transcript_{tag}_{re.sub('[^A-Za-z0-9]', '', aang['id'])}.json")
        log(f"Section {k + 1}/{len(sections)}: {ts(t0)}-{ts(t1)} ({ts(t1 - t0)}), audio {aang['name']}")
        feats = thumbs = db = None
        want_vis = sys.platform == "darwin" and not cfg.no_vision
        if os.path.exists(vcache) and not cfg.fresh:
            z = np.load(vcache, allow_pickle=True)
            tried = bool(z["vision_tried"]) if "vision_tried" in z.files else False
            if want_vis and not tried:
                log("  earlier analysis has no Apple Vision scores; redoing it once")
            else:
                feats, thumbs = list(z["feats"]), list(z["thumbs"])
                log("  reusing camera analysis")
        if feats is None:
            any_fresh = True
            ff = ff or find_ffmpeg()
            if want_vis:
                try:
                    import vision_mac
                    rep = vision_mac.self_test()
                    if rep.get("available") and rep.get("result_keys"):
                        log(f"  Apple Vision ready: {', '.join(rep['result_keys'])}"
                            + (f" (skipping: {rep['request_errors']})" if rep.get("request_errors") else ""))
                    else:
                        log(f"  Apple Vision problem: {rep.get('error') or 'not available'} {rep.get('request_errors') or ''}")
                        want_vis = False
                except Exception as e:
                    log(f"  Apple Vision problem: {e}")
                    want_vis = False
            with ThreadPoolExecutor(max_workers=min(A, os.cpu_count() or 2)) as ex:
                res = list(ex.map(lambda ang: analyze_angle(ff, ang, nbins, fps, t0, hw, want_vis), VA))
            feats, thumbs = [r[0] for r in res], [r[1] for r in res]
            np.savez_compressed(vcache, feats=np.array(feats, dtype=object), thumbs=np.array(thumbs, dtype=object),
                                vision_tried=want_vis)
        x = None
        if os.path.exists(acache) and not cfg.fresh:
            db = np.load(acache)["db"]
        else:
            any_fresh = True
            ff = ff or find_ffmpeg()
            log(f"  reading {aang['name']} audio")
            x, have = load_audio(ff, aang, t0, t1)
            db = loudness(x, have, nbins, fps)
            np.savez_compressed(acache, db=db)
        if not cfg.no_transcript and (cfg.fresh or not os.path.exists(tpath)):
            if x is None:
                ff = ff or find_ffmpeg()
                x, have = load_audio(ff, aang, t0, t1)
            try:
                tr = transcribe(x, t0, cfg.whisper)
            except Exception as e:
                log(f"  transcript failed ({e}); continuing without it")
                tr = None
            if tr is not None:
                with open(tpath, "w", encoding="utf-8") as fh:
                    json.dump(tr, fh, indent=1)
        x = None
        transcript = json.load(open(tpath, encoding="utf-8")) if os.path.exists(tpath) else None

        # Who is talking: compare the lav mics (folders named Groom / Bride / Officiant).
        role_db = {}
        rcache = os.path.join(outdir, f"roles_{tag}.npz")
        if proj.role_audio:
            if os.path.exists(rcache) and not cfg.fresh:
                zr = np.load(rcache)
                role_db = {k: zr[k] for k in zr.files}
            else:
                ff = ff or find_ffmpeg()
                for role, clips in proj.role_audio.items():
                    xr, hv = load_audio(ff, {"name": role, "clips": clips}, t0, t1, rate=8000)
                    if hv.any():
                        role_db[role] = loudness(xr, hv, nbins, fps, rate=8000)
                    del xr
                if role_db:  # don't cache "nothing found" (media may just be offline)
                    np.savez_compressed(rcache, **role_db)
        speech = np.zeros(nbins, bool)
        for sg in (transcript or []):
            end = min(sg["end"], sg["start"] + len(sg["text"]) / 13.0 + 1.5)
            i0_ = max(0, int((sg["start"] - t0) * fps)); i1_ = min(nbins, int((end - t0) * fps) + 1)
            speech[i0_:i1_] = True
        speaker = np.full(nbins, "", dtype=object)
        if role_db:
            roles_ = list(role_db)
            M = np.stack([np.nan_to_num(rolling(role_db[r], fps, np.nanmean), nan=-120.0) for r in roles_])
            floor = np.stack([np.nanpercentile(np.where(np.isfinite(role_db[r]), role_db[r], np.nan), 20)
                              if np.isfinite(role_db[r]).any() else -120.0 for r in roles_])
            top = M.argmax(axis=0)
            srt = np.sort(M, axis=0)
            lead = srt[-1] - (srt[-2] if len(roles_) > 1 else floor.max())
            loud = M[top, np.arange(nbins)] > floor[top] + 12
            ok_ = loud & (lead > 4) & (speech | (not transcript))
            speaker[ok_] = np.array(roles_, dtype=object)[top[ok_]]
            if not transcript:
                speech = speaker != ""

        bcache = os.path.join(outdir, f"blocked_{tag}.npz")
        if os.path.exists(bcache) and not cfg.fresh:
            zb = np.load(bcache)
            bl = [(zb[f"a{ai}"], zb[f"r{ai}"]) for ai in range(A)]
        else:
            log("  checking for people blocking the lens")
            bl = [blocked_frames(thumbs[ai], nbins, fps) for ai in range(A)]
            np.savez_compressed(bcache, **{f"a{ai}": bl[ai][0] for ai in range(A)},
                                **{f"r{ai}": bl[ai][1] for ai in range(A)})
        for ai in range(A):
            feats[ai]["marea"] = motion_area(thumbs[ai], nbins, fps)
            feats[ai]["block_area"], feats[ai]["block_rel"] = bl[ai]
        S = np.zeros((A, nbins), np.float32)
        U = np.zeros((A, nbins), bool)
        R, D = [], []
        for ai, ang in enumerate(VA):
            s_, u, r, d = score_angle(feats[ai], fps, bool(main_ang) and ang["id"] == main_ang["id"], cfg,
                                      roam_hint=ai in gims_cfg)
            S[ai], U[ai] = s_, u
            R.append(r)
            D.append(d)
        moments = [] if cfg.no_moments else find_moments(db, feats, fps, main_idx, cfg)

        # Music sections (processional, recessional, unity ceremony): 40s+ without
        # speech once people are talking. The camera doing the most smooth movement
        # there is the operator telling the story, so it gets the edge.
        music = []
        if transcript and A > 1 and cfg.music_bonus > 0:
            speech = np.zeros(nbins, bool)
            for sg in transcript:
                # Whisper often stretches a line across the music after it, so cap
                # each line at roughly how long it takes to say (~13 chars/sec).
                end = min(sg["end"], sg["start"] + len(sg["text"]) / 13.0 + 1.5)
                i0 = max(0, int((sg["start"] - t0) * fps)); i1 = min(nbins, int((end - t0) * fps) + 1)
                speech[i0:i1] = True
            quiet = ~speech
            i = 0
            while i < nbins:
                if quiet[i]:
                    j = i
                    while j < nbins and quiet[j]:
                        j += 1
                    if j - i >= int(cfg.music_min * fps):
                        music.append((i, j))
                    i = j
                else:
                    i += 1
            for i0, i1 in music:
                mv = [float(np.nanmean(np.nan_to_num(D[c]["pan"][i0:i1]) > 0.04)) for c in range(A)]
                op = int(np.argmax(mv))
                if mv[op] > 0.15:
                    S[op, i0:i1] += np.where(U[op, i0:i1], cfg.music_bonus, 0.0)
                    log(f"  music section {ts(t0 + i0 / fps)}-{ts(t0 + i1 / fps)}: favoring {VA[op]['name']} (moving {mv[op]:.0%} of it)")

        gims = set(gims_cfg) | {i for i in range(A) if D[i]["roaming"]}

        # Ceremony beats from the transcript: kiss, announcement, I do, rings.
        cues = ceremony_cues(transcript)
        key_windows = []
        # Grand exit: after the LAST announcement in the final 4 minutes, and after the kiss.
        ann = [c for c in cues if c[0] == "announce" and c[1] > t1 - 240]
        kiss_end = max([c[2] for c in cues if c[0] == "kiss"] or [0])
        exit_start = None
        if ann and kiss_end:  # ceremony only: receptions say "Mr. and Mrs." all night
            exit_start = max(ann[-1][2], kiss_end + 7.0)
        cues = [c for c in cues if c[0] != "announce"] + ([("announce", exit_start, exit_start, "")] if exit_start else [])
        exit_cams = set(gims)
        if exit_start and not exit_cams and A > 1:
            # No gimbal marked: the camera moving most during the exit is the operator
            # following the couple out.
            # Across the whole section: the camera that moves at all is the operator's
            # (walking backwards ahead of the couple is a dolly, which barely pans).
            mv = [float(np.mean(np.nan_to_num(D[c]["pan"]) > 0.03)) for c in range(A)]
            order = np.argsort(mv)[::-1]
            if mv[order[0]] > 0.05 and mv[order[0]] >= 3 * max(mv[order[1]], 0.005):
                exit_cams = {int(order[0])}
        for kind, cs, ce, txt in cues:
            if kind == "announce" and exit_cams:
                # From the announcement to the end of the section (max 75s): the grand exit.
                i0_ = max(0, int((ce - t0) * fps)); i1_ = min(nbins, i0_ + int(75 * fps))
                key_windows.append(("Grand exit", i0_, i1_))
                for g in exit_cams:
                    ok = D[g]["relaxed"][i0_:i1_]
                    U[g, i0_:i1_] |= ok
                    S[g, i0_:i1_] = np.where(ok, np.maximum(S[g, i0_:i1_], 1.0) + cfg.exit_bonus, S[g, i0_:i1_])
                log(f"  grand exit from {ts(ce)}: favoring {', '.join(VA[g]['name'] for g in exit_cams)}")
            elif kind == "kiss":
                i0_ = max(0, int((ce - t0) * fps)); i1_ = min(nbins, i0_ + int(9 * fps))
                key_windows.append(("Kiss", i0_, i1_))
                for c in couple_cams:  # the kiss itself: tight on the couple
                    S[c, i0_:i1_] += np.where(U[c, i0_:i1_], 1.5, 0.0)
            elif kind == "ido":
                i0_ = max(0, int((cs - t0) * fps) - fps); i1_ = min(nbins, int((ce - t0) * fps) + 2 * fps)
                key_windows.append(("I do", i0_, i1_))
            elif kind == "rings":
                i0_ = max(0, int((cs - t0) * fps)); i1_ = min(nbins, i0_ + int(20 * fps))
                key_windows.append(("Rings", i0_, i1_))
        for nm_, i0_, i1_ in key_windows:
            moments.append((i0_, min(i1_, i0_ + 3 * fps), "cue"))

        # Stay on whoever is talking (their face camera), unless something better happens.
        if speaker.any() and cam_role:
            for r in ("bride", "groom", "officiant"):
                m = speaker == r
                if not m.any():
                    continue
                for c in face_cam[r]:
                    S[c] += np.where(m & U[c], cfg.speaker_bonus, 0.0)
                if r == "officiant" and not face_cam["officiant"]:
                    for c in couple_cams:  # officiant talking: watch the couple listen
                        S[c] += np.where(m & U[c], 0.3, 0.0)

        # Nobody in frame (trees, sky, empty floor) while another camera has people:
        # fine as a quick transition, not as a held shot. Needs Apple Vision.
        hum = [np.nan_to_num(feats[c].get("humans", np.full(nbins, np.nan))) for c in range(A)]
        if any(np.isfinite(feats[c].get("humans", np.array([np.nan]))).any() for c in range(A)):
            for c in range(A):
                others = np.max([hum[o] for o in range(A) if o != c] or [np.zeros(nbins)], axis=0)
                empty = (rolling(hum[c].astype(np.float32), 2 * fps, np.nanmax) == 0) & (others > 0)
                S[c] -= np.where(empty, cfg.empty_penalty, 0.0)

        # Busy vs calm: walk-ins, laughs, music sections cut fast; plain talking holds.
        busy = np.zeros(nbins, bool)
        for c in range(A):
            busy |= np.nan_to_num(D[c].get("action", np.zeros(nbins))) > 0.35
        for i0_, i1_ in music:
            busy[i0_:i1_] = True
        for a_, b_, k_ in moments:
            busy[max(0, a_ - fps):min(nbins, b_ + 3 * fps)] = True
        calm = speech & ~busy

        locks, named = [], []
        lean = np.zeros((A, nbins), np.float32)
        if brain:
            def bins(m):
                return (max(0, int((parse_clock(str(m["start"])) - t0) * fps)),
                        min(nbins, int(np.ceil((parse_clock(str(m["end"])) - t0) * fps))))
            for m in brain.get("avoid", []):
                ai = names.get(str(m.get("angle", "")).lower())
                i0, i1 = bins(m)
                if ai is not None and i1 > i0:
                    S[ai, i0:i1] -= 10
            for m in brain.get("moments", []):
                i0, i1 = bins(m)
                if i1 <= i0:
                    continue
                if m.get("marker", True):
                    named.append((t0 + i0 / fps, t0 + i1 / fps, m.get("label", "Moment"), m.get("angle")))
                ai = names.get(str(m.get("angle", "")).lower())
                if ai is None:
                    continue
                if m.get("lock"):
                    locks.append((i0, i1, ai))
                else:
                    wgt = float(m.get("weight", 10.0))  # small weight = "lean on", big = "use this"
                    S[ai, i0:i1] += np.where(U[ai, i0:i1], wgt, 0.0)
                    if wgt <= 1.0:  # a lean sets home base; it must not block cutaways
                        lean[ai, i0:i1] += np.where(U[ai, i0:i1], wgt, 0.0)

        moment_info = []
        for a, b, kind in moments:
            ta, tb = t0 + a / fps, t0 + b / fps
            if kind == "cue" or any(na <= tb and ta <= nb for na, nb, _, _ in named):
                continue
            lo, hi = max(0, a - fps // 2), min(nbins, b + 3 * fps)
            react = []
            # Laughs and applause: the couple's faces first, then anyone with faces.
            pool = couple_cams if couple_cams else [c for c in range(A) if c != main_idx]
            for c in pool:
                if not U[c, lo:hi].mean() > 0.7:
                    continue
                fs = np.nan_to_num(D[c]["faces"][lo:hi]).mean() + 3 * np.nan_to_num(D[c]["facesize"][lo:hi]).mean()
                spk = speaker[lo:hi]
                # the person NOT talking is the one reacting
                if cam_role.get(c) in ("bride", "groom") and (spk == cam_role.get(c)).mean() > 0.5:
                    fs -= 0.5
                react.append((fs, c))
            best = max(react)[1] if react else None
            if best is not None:
                S[best, lo:hi] += cfg.reaction_bonus
            if kind == "cue":
                continue
            what = "Laughter / applause?" if kind == "audio" else "Reaction?"
            moment_info.append((ta, tb, what, VA[best]["name"] if best is not None else None))

        sw = np.where(calm, cfg.switch * cfg.calm_switch, cfg.switch) * fps
        min_bins = np.where(calm, int(cfg.calm_min_shot * fps), max(1, int(cfg.min_shot * fps)))
        max_bins = np.where(calm, int(cfg.calm_max_shot * fps), int(cfg.max_shot * fps))
        locked = np.zeros(nbins, bool)
        for i0, i1, ai in locks:
            locked[i0:i1] = True
        path = viterbi(S, sw)
        path = enforce_min(path, S, U, min_bins)
        path, n_earn = earn_cuts(path, S, U, fps, cfg.earn_margin, locked=locked, max_hold=max_bins)
        if n_earn:
            log(f"  dropped {n_earn} cut(s) to a shot that wasn't better")
        fszA = np.array([np.asarray(D[c]["facesize"], np.float32) for c in range(A)])
        path = add_cutaways(path, S - lean, U, main_idx, min_bins, max_bins, cfg.cutaway_drop, fps,
                            prefer=gims | set(couple_cams), locked=locked, fsz=fszA,
                            boredom=cfg.boredom, variety=cfg.variety_bonus)
        dbg = os.environ.get("AUTOCUT_DEBUG")
        if dbg:
            log(f"    gims={sorted(gims)} before sprinkle: {[(round(a/fps,1), VA[g]['name']) for a, b, g in runs(path)]}")
        path = gimbal_sprinkle(path, S, U, gims, fps, cfg.gimbal_every, locked=locked, min_bins=min_bins)
        if dbg:
            log(f"    after sprinkle: {[(round(a/fps,1), VA[g]['name']) for a, b, g in runs(path)]}")
        path = enforce_min(path, S, U, min_bins)
        for i0, i1, ai in locks:
            path[i0:i1] = ai
        for fr in cfg.force:
            rng, nm = fr.split("=", 1)
            a, b = rng.split("-")
            ai = names.get(nm.strip().strip('"').lower())
            if ai is None:
                sys.exit(f"--force angle '{nm}' not found")
            i0 = max(0, int((parse_clock(a) - t0) * fps))
            i1 = min(nbins, int((parse_clock(b) - t0) * fps))
            if i1 > i0:
                path[i0:i1] = ai
                locked[i0:i1] = True
        if locked.any():
            path = enforce_min(path, S, U, min_bins, locked)
        path, mv_ = clean_cuts(path, feats, U, fps, locked if locked.any() else None)
        path = enforce_min(path, S, U, int(1.5 * fps), locked if locked.any() else None)  # no slivers
        # last word: a quick there-and-back blip has to be clearly the better shot
        softA = np.array([np.nan_to_num(np.asarray(D[c]["focus"], np.float32), nan=1.0) < 0.7 for c in range(A)])
        path, n_blip = earn_cuts(path, S, U, fps, cfg.earn_margin, max_len_s=3.0,
                                 locked=locked if locked.any() else None, sandwich_only=True, max_hold=max_bins,
                                 soft=softA)
        if n_blip:
            log(f"  dropped {n_blip} quick blip(s) to a shot that wasn't better")
        if mv_["earlier"] or mv_["later"]:
            log(f"  cleaner cuts: {mv_['earlier']} moved earlier (before a camera move, {mv_['seconds']:.1f}s total), "
                f"{mv_['later']} wait for a move to settle")
        roam = [VA[i]["name"] for i in range(A) if D[i]["roaming"]]
        if cfg.sheets:   # review contact sheets: only when asked for
            make_sheets(thumbs, path, VA, fps, nbins, review_dir, t0, R)
        for nm_, i0_, i1_ in (key_windows if cfg.sheets else []):
            za = t0 + max(0, i0_ - 2 * fps) / fps
            for part in range(0, min(3, int(np.ceil((i1_ - i0_) / (10 * fps))) + 1)):
                zs = za + part * 10
                if zs < t1:
                    zoom_sheet(thumbs, VA, fps, t0, zs, min(t1, zs + 10), path,
                               os.path.join(review_dir, f"key_{ts(zs).replace(':', '-')}_{nm_.replace(' ', '')}.jpg"),
                               reasons=R, big=True)
        for zr in cfg.zoom:
            za, zb = [parse_clock(x) for x in zr.split("-")]
            if t0 <= za < t1:
                zb = min(zb, t1)
                zoom_sheet(thumbs, VA, fps, t0, za, zb, path,
                           os.path.join(review_dir, f"zoom_{ts(za).replace(':', '-')}.jpg"), reasons=R)
        if want_vis:
            nv = [int(np.isfinite(feats[i].get("fq", np.array([np.nan]))).sum()) for i in range(A)]
            log(f"  Apple Vision frames scored per camera: {nv}"
                + (f" (last error: {vision_mac.last_error})" if not all(nv) and 'vision_mac' in sys.modules else ""))
        gap = np.zeros(nbins, bool)
        if cfg.remove_gaps:
            pic = np.zeros(nbins, bool)
            for c in range(A):
                cov = ~np.isnan(feats[c]["sharp"])
                pic |= cov & (np.nan_to_num(feats[c]["luma"], nan=0) > 0.04)
                # A clip sits here in the timeline but couldn't be read (offline drive,
                # missing proxy): unknown, so never cut it.
                for cl in VA[c]["clips"]:
                    if not cl["video"]:
                        continue
                    i0_ = max(0, int(np.ceil((cl["off"] - t0) * fps)))
                    i1_ = min(nbins, int((cl["off"] + cl["dur"] - t0) * fps))
                    if i1_ > i0_:
                        pic[i0_:i1_] |= ~cov[i0_:i1_]
            nopic = ~pic
            for a_, b_, v_ in runs(nopic.astype(np.int16)):
                if v_ == 1 and (b_ - a_) >= int(cfg.gap_min * fps):
                    gap[a_:b_] = True
            if gap.any():
                log(f"  no camera has video for {gap.sum() / fps:.1f}s here: cutting it out")
        if speaker.any():
            sp_share = {r: round(float((speaker == r).mean()) * 100) for r in set(speaker) if r}
            log(f"  who's talking (share of section): {sp_share}")
        results.append(dict(t0=t0, t1=t1, nbins=nbins, audio=aang, path=path, S=S, U=U, R=R, D=D,
                            speaker=speaker, calm=calm, cam_role=cam_role, gims=sorted(gims), gap=gap,
                            moments=moments, moment_info=moment_info, named=named, transcript=transcript, roam=roam))

    if not results:
        sys.exit("Nothing long enough to cut.")
    roam_all = sorted({n for r in results for n in r["roam"]})
    if roam_all:
        log(f"Roaming camera(s) detected: {', '.join(roam_all)}. Smooth moves allowed there.")

    def cut_fn(a, b):
        """Shots covering [a, b) (multicam seconds) as (start, end, angleID)."""
        for r in results:
            if r["t0"] - 0.5 <= a and b <= r["t1"] + 0.5:
                i0 = max(0, int(round((a - r["t0"]) * fps)))
                i1 = min(r["nbins"], int(round((b - r["t0"]) * fps)))
                if i1 <= i0:
                    break
                out = []
                lab = r["path"][i0:i1].astype(np.int32).copy()
                lab[r["gap"][i0:i1]] = -1
                for x0, x1, ang in runs(lab):
                    out.append((r["t0"] + (i0 + x0) / fps, r["t0"] + (i0 + x1) / fps,
                                GAP if ang < 0 else VA[ang]["id"]))
                out[0] = (a, out[0][1], out[0][2])
                out[-1] = (out[-1][0], b, out[-1][2])
                return out
        return [(a, b, None)]

    markers = []
    for r in results:
        markers += [(a, lab) for a, b, lab, ang in r["named"]]
        markers += [(a, f"{w} ({b - a:.1f}s)" + (f" best: {x}" if x else "")) for a, b, w, x in r["moment_info"]]

    xml_path = os.path.join(outdir, f"{base} Multicam Flow.fcpxml")
    suffix = " Multicam Flow" + (" (edited)" if brain else "")
    if inplace:
        def cut_ids(a, b):
            return [(x, y, aid) for x, y, aid in cut_fn(a, b) if aid] or []
        stats = proj.write_inplace(lambda a, b: [(x, y, aid) for x, y, aid in cut_fn(a, b)
                                                 if aid] or [(a, b, None)],
                                   xml_path, markers, suffix=suffix, min_shot=cfg.min_shot,
                                   audio_id=audio_override["id"] if audio_override else None)
        if stats.get("gaps_removed"):
            log(f"Cut out {stats['gaps_removed']} no-video gap(s), {stats['gap_seconds']:.1f}s total")
        log(f"Recut {stats['sections']} multicam clip(s) into {stats['pieces']} shots"
            + (f"; left {stats['skipped']} untouched (split edits or very short)" if stats["skipped"] else ""))
    else:
        r = results[0]
        lab = r["path"].astype(np.int32).copy()
        lab[r["gap"]] = -1
        out_shots = [(r["t0"] + a / fps, r["t0"] + b / fps, VA[ang] if ang >= 0 else None) for a, b, ang in runs(lab)]
        out_shots[-1] = (out_shots[-1][0], r["t1"], out_shots[-1][2])
        out_shots = [x for x in out_shots if x[2] is not None]
        proj.write(out_shots, r["audio"], xml_path, f"{proj.media.get('name', base)}{suffix}", r["t0"], markers)
        stats = None

    # Report + brief
    csv_path = os.path.join(outdir, "cuts.csv")
    usage = {a["name"]: 0.0 for a in VA}
    flagged, ncuts = [], 0
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "start", "end", "length", "angle", "faces", "focus", "movement", "skipped angles"])
        for si, r in enumerate(results, 1):
            for a, b, ang in runs(r["path"]):
                ncuts += 1
                mid = slice(a, b)
                d = r["D"][ang]
                skipped = []
                for oi in range(A):
                    if oi == ang:
                        continue
                    rs_ = [x for x in r["R"][oi][mid] if x]
                    if rs_:
                        skipped.append(f"{VA[oi]['name']}: {max(set(rs_), key=rs_.count)}")
                if not r["U"][ang, mid].all():
                    flagged.append((r["t0"] + a / fps, r["t0"] + b / fps, VA[ang]["name"]))
                usage[VA[ang]["name"]] += (b - a) / fps
                w.writerow([si, ts(r["t0"] + a / fps), ts(r["t0"] + b / fps), f"{(b - a) / fps:.1f}", VA[ang]["name"],
                            f"{np.nanmax(d['faces'][mid]) if np.isfinite(d['faces'][mid]).any() else 0:.0f}",
                            f"{np.nanmedian(d['focus'][mid]):.2f}", f"{np.nanmedian(d['pan'][mid]):.3f}",
                            "; ".join(skipped)])
    write_brief(os.path.join(outdir, "brief.md"), proj, VA, main_ang, results, fps, inplace)
    all_tr = [l for r in results for l in (r["transcript"] or [])]
    with open(os.path.join(outdir, "transcript.json"), "w", encoding="utf-8") as fh:
        json.dump(all_tr, fh, indent=1)

    summary = {
        "multicam": proj.media.get("name"), "mode": "recut in place" if inplace else "new project",
        "projects": proj.project_names, "sections": len(results), "length": ts(total_len),
        "angles": [a["name"] for a in VA], "main": main_name,
        "audio": "timeline (per section)" if keep_audio and inplace else results[0]["audio"]["name"],
        "roaming_cameras": roam_all, "manual_decisions": bool(brain), "cuts": ncuts,
        "avg_shot_sec": round(total_len / max(1, ncuts), 1),
        "screen_time": {k: ts(v) for k, v in usage.items()},
        "transcript_lines": len(all_tr),
        "named_moments": [f"{ts(a)} {lab}" for r in results for a, b, lab, ang in r["named"]],
        "loudness_moments": [f"{ts(a)} {w}" + (f" -> {x}" if x else " (no clean reaction angle)")
                             for r in results for a, b, w, x in r["moment_info"]][:80],
        "no_clean_angle": [f"{ts(a)}-{ts(b)} ({n})" for a, b, n in flagged][:40],
        "inplace_stats": stats,
        "fcpxml": xml_path, "elapsed_sec": round(time.time() - t_start, 1),
    }
    with open(os.path.join(outdir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
