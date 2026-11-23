"""Apple Vision on macOS: frame quality, face quality and people, on-device.

Used by engine.py when running natively on a Mac. Everything here fails soft:
if Vision or a request isn't available, the caller just gets fewer keys back.
"""

import subprocess
import sys

_ready = None
last_error = ""
_Vision = None
_NSData = None
_AES = None


def available():
    """Import Apple Vision via PyObjC, installing it once if needed."""
    global _ready, _Vision, _NSData, _AES, last_error
    if _ready is not None:
        return _ready
    _ready = False
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa
    except Exception:
        try:
            print("  installing Apple Vision bridge (one time) ...", file=sys.stderr, flush=True)
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyobjc-framework-Vision"],
                           check=True, capture_output=True)
        except Exception as e:
            last_error = f"install failed: {e}"
            print(f"  note: Apple Vision unavailable ({e})", file=sys.stderr)
            return False
    try:
        import Vision
        from Foundation import NSData
        _Vision, _NSData = Vision, NSData
        _AES = getattr(Vision, "VNCalculateImageAestheticsScoresRequest", None)
        _ready = True
        if _AES is None:
            print("  note: this macOS has no aesthetics scoring; using faces and people only",
                  file=sys.stderr)
    except Exception as e:
        last_error = f"import failed: {e}"
        print(f"  note: Apple Vision unavailable ({e})", file=sys.stderr)
    return _ready


_req_errors = {}


def _run(handler, req, name):
    """Perform one request on its own; a failure in one never blocks the others."""
    try:
        res = handler.performRequests_error_([req], None)
        ok = res[0] if isinstance(res, tuple) else res
        if not ok:
            _req_errors[name] = "request returned false"
            return None
        return req.results() or []
    except Exception as e:
        _req_errors[name] = f"{type(e).__name__}: {e}"[:160]
        return None


def _num(x):
    try:
        return float(x)
    except Exception:
        try:
            return float(x.floatValue())
        except Exception:
            return None


def analyze(jpeg_bytes):
    """Returns dict with any of: aes (-1..1), util (bool), fq (0..1 best face quality),
    fsz (largest face width, 0..1 of frame), nfaces, humans, hsz (largest person height)."""
    global last_error
    if not available():
        return {}
    out = {}
    try:
        data = _NSData.dataWithBytes_length_(jpeg_bytes, len(jpeg_bytes))
        handler = _Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    except Exception as e:
        last_error = f"handler failed: {type(e).__name__}: {e}"
        out["err"] = last_error[:200]
        return out

    if _AES is not None and "aes" not in _disabled:
        r = _run(handler, _AES.alloc().init(), "aesthetics")
        if r:
            try:
                o = r[0]
                sc = _num(o.overallScore())
                if sc is not None:
                    out["aes"] = sc
                try:
                    out["util"] = bool(o.isUtility())
                except Exception:
                    pass
            except Exception as e:
                _req_errors["aesthetics"] = f"{type(e).__name__}: {e}"[:160]

    if "faces" not in _disabled:
        r = _run(handler, _Vision.VNDetectFaceCaptureQualityRequest.alloc().init(), "faces")
        if r is not None:
            best = 0.0
            size = 0.0
            for f in r:
                try:
                    q = _num(f.faceCaptureQuality())
                    best = max(best, q or 0.0)
                except Exception:
                    pass
                try:
                    size = max(size, float(f.boundingBox().size.width))
                except Exception:
                    pass
            out["fq"] = best
            out["fsz"] = size
            out["nfaces"] = len(r)
        else:
            # older request name fallback: plain face rectangles
            r2 = _run(handler, _Vision.VNDetectFaceRectanglesRequest.alloc().init(), "faces_basic")
            if r2 is not None:
                out["fq"] = 0.5 if r2 else 0.0
                out["fsz"] = max([float(f.boundingBox().size.width) for f in r2] or [0.0])
                out["nfaces"] = len(r2)

    if "people" not in _disabled:
        r = _run(handler, _Vision.VNDetectHumanRectanglesRequest.alloc().init(), "people")
        if r is not None:
            out["humans"] = len(r)
            try:
                out["hsz"] = max([float(h.boundingBox().size.height) for h in r] or [0.0])
            except Exception:
                pass

    if _req_errors:
        last_error = "; ".join(f"{k}: {v}" for k, v in _req_errors.items())
    if not any(k in out for k in ("aes", "fq", "humans")):
        out["err"] = last_error[:200] or "no results"
    return out


_disabled = set()


def self_test():
    """Run every request on a synthetic frame; report what works."""
    import numpy as np, cv2
    img = np.full((270, 480, 3), 120, np.uint8)
    cv2.circle(img, (240, 120), 60, (200, 180, 160), -1)
    ok, jpg = cv2.imencode(".jpg", img)
    rep = {"platform": sys.platform, "python": sys.version.split()[0], "available": available(),
           "aesthetics_supported": _AES is not None, "error": last_error}
    if rep["available"]:
        r = analyze(jpg.tobytes())
        rep["result_keys"] = sorted(k for k in r if k != "err")
        rep["request_errors"] = dict(_req_errors)
        rep["error"] = r.get("err", "")
    return rep
