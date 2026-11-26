#!/usr/bin/env python3
"""
AbuAsrh Cloud Studio Backend (FastAPI + VapourSynth f0e/blur v2.45 + FFmpeg).
Runs on Railway so mobile/desktop users get real optical-flow motion blur
and instant zero-reencode 60/120 FPS container tuning with zero browser crashes.
"""
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
JOBS_ROOT = Path(os.environ.get("JOBS_ROOT", "/tmp/abuasrh_jobs"))
BLUR_ENV = Path(os.environ.get("BLUR_ENV", "/opt/blur_env"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
JOB_TTL_SECONDS = 30 * 60
RENDER_SEM = threading.Semaphore(2)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
_SVP_READY = False

app = FastAPI(title="AbuAsrh Cloud Studio", version="4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def parse_time_s(t: str) -> float:
    parts = t.strip().split(":")
    if len(parts) == 3:
        return 3600.0 * float(parts[0] or 0) + 60.0 * float(parts[1] or 0) + float(parts[2] or 0)
    return 0.0


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            text=True,
            timeout=15,
        ).strip()
        return max(0.0, float(out or 0.0))
    except Exception:
        return 0.0


def compute_blur_weights_str(frames: int, cfg: dict) -> str:
    def norm(arr: list[float]) -> list[float]:
        mn = min(arr)
        shifted = [w - mn + 1.0 for w in arr] if mn < 0 else arr
        tot = sum(shifted) or 1.0
        return [w / tot for w in shifted]

    def scale_range(n: int, start: float, end: float) -> list[float]:
        if n <= 1:
            return [start]
        return [start + i * (end - start) / (n - 1) for i in range(n)]

    def parse_bound(s: str) -> tuple[float, float]:
        try:
            raw = s.strip()
            arr = json.loads(raw if raw.startswith("[") else f"[{raw}]")
            return float(arr[0]), float(arr[1])
        except Exception:
            return 0.0, 2.0

    def gauss(n: int, mean: float, std: float, bound: tuple[float, float]) -> list[float]:
        xs = scale_range(n, bound[0], bound[1])
        s = std or 1.0
        return norm([math.exp(-((x - mean) ** 2) / (2.0 * s * s)) for x in xs])

    t = str(cfg.get("blur_weighting") or "gaussian_sym")
    bound = parse_bound(str(cfg.get("blur_weighting_gaussian_bound") or "[0,2]"))
    std = float(cfg.get("blur_weighting_gaussian_std_dev") or 1.0)
    mean = float(cfg.get("blur_weighting_gaussian_mean") or 2.0)

    if t == "equal":
        w = [1.0 / frames] * frames
    elif t == "ascending":
        w = norm([float(i + 1) for i in range(frames)])
    elif t == "descending":
        w = norm([float(frames - i) for i in range(frames)])
    elif t == "pyramid":
        h = (frames - 1) / 2.0
        w = norm([h - abs(i - h) + 1.0 for i in range(frames)])
    elif t == "vegas":
        w = norm([1.0] + [2.0] * max(0, frames - 2) + [1.0]) if frames % 2 == 0 else [1.0 / frames] * frames
    elif t == "gaussian":
        w = gauss(frames, mean, std, bound)
    elif t == "gaussian_reverse":
        w = list(reversed(gauss(frames, mean, std, bound)))
    elif t == "gaussian_sym":
        m = max(abs(bound[0]), abs(bound[1]))
        w = gauss(frames, 0.0, std, (-m, m))
    else:
        custom = []
        for x in t.split(","):
            try:
                custom.append(float(x.strip()))
            except ValueError:
                pass
        if custom:
            idxs = scale_range(frames, 0.0, len(custom) - 0.1)
            w = norm([custom[int(idx)] for idx in idxs])
        else:
            w = [1.0 / frames] * frames

    return " ".join(str(max(1, round(v * 1000))) for v in w)


def stream_process(proc: subprocess.Popen, job: dict, duration_s: float) -> int:
    buf = b""
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(256)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf or b"\r" in buf:
            idx_n = buf.find(b"\n")
            idx_r = buf.find(b"\r")
            idxs = [x for x in (idx_n, idx_r) if x >= 0]
            cut = min(idxs)
            line = buf[:cut].decode("utf-8", errors="replace").strip()
            buf = buf[cut + 1 :]
            if not line:
                continue
            with JOBS_LOCK:
                logs = job["logs_tail"]
                logs.append(line)
                if len(logs) > 30:
                    del logs[0]

                m_vs = re.search(r"Frame:\s*(\d+)/(\d+)", line)
                if m_vs:
                    cur, tot = int(m_vs.group(1)), max(1, int(m_vs.group(2)))
                    pct = max(5, min(96, int((cur / tot) * 90) + 5))
                    job["progress"] = max(job["progress"], pct)
                    job["stage"] = f"VapourSynth {cur}/{tot}"
                else:
                    m_tm = re.search(r"time=(\d+:\d+:\d+(?:\.\d+)?)", line)
                    if m_tm:
                        t_s = parse_time_s(m_tm.group(1))
                        if duration_s > 0:
                            pct = max(5, min(98, int((t_s / duration_s) * 95)))
                            job["progress"] = max(job["progress"], pct)
                        job["stage"] = f"Encoding {m_tm.group(1)}"
    return proc.wait()


def save_upload_to_disk(upload: UploadFile, dest: Path) -> int:
    total = 0
    with dest.open("wb") as fh:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="حجم الفيديو يتجاوز الحد الأقصى المسموح (500MB).")
            fh.write(chunk)
    return total


def cleanup_loop():
    while True:
        time.sleep(120)
        now = time.time()
        expired = []
        with JOBS_LOCK:
            for jid, job in list(JOBS.items()):
                if job["status"] in ("done", "error", "cancelled") and now - job.get("updated_at", now) > JOB_TTL_SECONDS:
                    expired.append((jid, Path(job["job_dir"])))
            for jid, _ in expired:
                JOBS.pop(jid, None)
        for _, d in expired:
            shutil.rmtree(d, ignore_errors=True)


def _probe_svp_ready() -> bool:
    try:
        code = (
            "import vapoursynth as vs; c = vs.core; "
            "c.std.LoadPlugin('/opt/blur_env/plugins/libsvpflow1_vs64.so'); "
            "c.std.LoadPlugin('/opt/blur_env/plugins/libsvpflow2_vs64.so'); "
            "print('1' if (hasattr(c, 'svp1') and hasattr(c, 'svp2')) else '0')"
        )
        out = subprocess.check_output([sys.executable, "-c", code], text=True, timeout=10).strip()
        return out.endswith("1")
    except Exception:
        return False


@app.on_event("startup")
def on_startup():
    global _SVP_READY
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    _SVP_READY = _probe_svp_ready()
    t = threading.Thread(target=cleanup_loop, daemon=True)
    t.start()


@app.get("/api/health")
def health():
    vs_ok = shutil.which("vspipe") is not None
    blur_ok = (BLUR_ENV / "scripts" / "blur.py").exists()
    return {
        "ok": True,
        "version": "4.0",
        "vapoursynth": vs_ok,
        "blur_env": blur_ok,
        "svp": _SVP_READY,
    }


def _run_instant_worker(job_id: str, in_path: Path, job_dir: Path, fps: int, slow_mo: bool):
    job = JOBS[job_id]
    out_path = job_dir / f"AbuAsrh_{fps}fps.mp4"
    duration_s = probe_duration(in_path)

    def build_cmd(audio_copy: bool) -> list[str]:
        cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-stats"]
        if slow_mo:
            cmd += ["-itsscale", "2"]
        cmd += [
            "-i", str(in_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "copy",
            *(["-c:a", "copy"] if audio_copy else ["-c:a", "aac", "-b:a", "192k"]),
            "-video_track_timescale", "120000" if fps == 120 else "60000",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-color_range", "tv",
            "-map_metadata", "-1",
            "-map_metadata:s:v", "-1",
            "-map_metadata:s:a", "-1",
            "-map_chapters", "-1",
            "-metadata", "title=AbuAsrh",
            "-metadata", "artist=AbuAsrh",
            "-metadata", f"comment=AbuAsrh Cloud {fps}FPS",
            "-brand", "mp42",
            "-movflags", "+faststart",
            str(out_path),
        ]
        return cmd

    with RENDER_SEM:
        with JOBS_LOCK:
            if job["cancelled"]:
                job["status"] = "cancelled"
                job["updated_at"] = time.time()
                return
            job["status"] = "running"
            job["stage"] = f"Processing {fps} FPS..."
            job["progress"] = 10

        try:
            proc = subprocess.Popen(
                build_cmd(True),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            with JOBS_LOCK:
                job["proc"] = proc
            rc = stream_process(proc, job, duration_s)

            if rc != 0 and not job["cancelled"]:
                if out_path.exists():
                    out_path.unlink(missing_ok=True)
                with JOBS_LOCK:
                    job["stage"] = "Retrying audio as AAC..."
                proc2 = subprocess.Popen(
                    build_cmd(False),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                with JOBS_LOCK:
                    job["proc"] = proc2
                rc = stream_process(proc2, job, duration_s)

            with JOBS_LOCK:
                job["proc"] = None
                if job["cancelled"]:
                    job["status"] = "cancelled"
                elif rc == 0 and out_path.exists():
                    job["status"] = "done"
                    job["progress"] = 100
                    job["stage"] = "Complete"
                    job["output_path"] = str(out_path)
                    job["output_name"] = out_path.name
                    job["output_size"] = out_path.stat().st_size
                    job["elapsed_s"] = round(time.time() - job["created_at"], 2)
                else:
                    job["status"] = "error"
                    job["error"] = f"FFmpeg exited with code {rc}"
                job["updated_at"] = time.time()
        except Exception as exc:
            with JOBS_LOCK:
                job["proc"] = None
                job["status"] = "error"
                job["error"] = str(exc)
                job["updated_at"] = time.time()
        finally:
            try:
                in_path.unlink(missing_ok=True)
            except OSError:
                pass


def _run_ffmpeg_blur_fallback(job: dict, in_path: Path, out_path: Path, cfg: dict, duration_s: float) -> int:
    out_fps = max(15, min(240, int(cfg.get("blur_output_fps") or 60)))
    vf = []
    if cfg.get("timescale") and float(cfg.get("input_timescale") or 1.0) != 1.0:
        vf.append(f"setpts=PTS/{float(cfg['input_timescale'])}")

    working_fps = out_fps
    if cfg.get("interpolate", True):
        raw_fps = str(cfg.get("interpolated_fps") or "600").strip()
        if raw_fps.endswith("x"):
            target = round(out_fps * (float(raw_fps[:-1] or 3)))
        else:
            target = int(float(raw_fps or 180))
        working_fps = max(out_fps * 2, min(180, target))
        vf.append(f"minterpolate=fps={working_fps}:mi_mode=mci:mc_mode=obmc:me_mode=bidir:me=epzs")

    amt = float(cfg.get("blur_amount") or 1.0)
    if cfg.get("blur", True) and amt > 0:
        gap = max(2, round(working_fps / out_fps))
        frames = max(3, round(gap * amt) + 1)
        if frames % 2 == 0:
            frames += 1
        frames = min(15, frames)
        weights = compute_blur_weights_str(frames, cfg)
        gamma = float(cfg.get("blur_gamma") or 1.0)
        if gamma != 1.0:
            vf.append(f"eq=gamma={gamma}")
        vf.append(f"tmix=frames={frames}:weights={weights}")
        if gamma != 1.0:
            vf.append(f"eq=gamma={1.0 / gamma:.3f}")

    vf.append(f"fps={out_fps}")
    if cfg.get("timescale") and float(cfg.get("output_timescale") or 1.0) != 1.0:
        vf.append(f"setpts=PTS/{float(cfg['output_timescale'])}")
    if cfg.get("filters"):
        br = float(cfg.get("brightness") or 1.0) - 1.0
        sat = float(cfg.get("saturation") or 1.0)
        cont = float(cfg.get("contrast") or 1.0)
        vf.append(f"eq=brightness={br:.3f}:saturation={sat}:contrast={cont}")

    crf = int(cfg.get("quality") or 18)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning", "-stats",
        "-i", str(in_path),
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-metadata", "title=AbuAsrh Blur", "-metadata", "artist=AbuAsrh",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with JOBS_LOCK:
        job["proc"] = proc
        job["stage"] = "FFmpeg MCI Fallback..."
    return stream_process(proc, job, duration_s)


def _run_blur_worker(job_id: str, in_path: Path, job_dir: Path, cfg: dict):
    job = JOBS[job_id]
    duration_s = probe_duration(in_path)
    container = str(cfg.get("video_container") or "mp4").lower()
    if container not in ("mp4", "mkv", "mov", "avi"):
        container = "mp4"
    out_fps = int(cfg.get("blur_output_fps") or 60)
    expected_vs_out = job_dir / f"output_blur.{container}"
    final_name = f"AbuAsrh_Blur_{out_fps}fps.{container}"
    final_path = job_dir / final_name

    with RENDER_SEM:
        with JOBS_LOCK:
            if job["cancelled"]:
                job["status"] = "cancelled"
                job["updated_at"] = time.time()
                return
            job["status"] = "running"
            job["stage"] = "Starting f0e/blur VapourSynth..."
            job["progress"] = 5

        try:
            cfg_b64 = base64.b64encode(json.dumps(cfg).encode("utf-8")).decode("ascii")
            cmd = [
                sys.executable,
                str(BASE_DIR / "scripts" / "render_blur.py"),
                "--input", str(in_path),
                "--output-dir", str(job_dir),
                "--blur-env", str(BLUR_ENV),
                "--config-b64", cfg_b64,
                "--job-id", job_id,
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            with JOBS_LOCK:
                job["proc"] = proc
            rc = stream_process(proc, job, duration_s)

            if (rc != 0 or not expected_vs_out.exists()) and not job["cancelled"]:
                if expected_vs_out.exists():
                    expected_vs_out.unlink(missing_ok=True)
                rc = _run_ffmpeg_blur_fallback(job, in_path, expected_vs_out, cfg, duration_s)

            with JOBS_LOCK:
                job["proc"] = None
                if job["cancelled"]:
                    job["status"] = "cancelled"
                elif rc == 0 and expected_vs_out.exists():
                    expected_vs_out.rename(final_path)
                    job["status"] = "done"
                    job["progress"] = 100
                    job["stage"] = "Complete"
                    job["output_path"] = str(final_path)
                    job["output_name"] = final_name
                    job["output_size"] = final_path.stat().st_size
                    job["elapsed_s"] = round(time.time() - job["created_at"], 2)
                else:
                    job["status"] = "error"
                    job["error"] = f"Blur render failed (exit code {rc})"
                job["updated_at"] = time.time()
        except Exception as exc:
            with JOBS_LOCK:
                job["proc"] = None
                job["status"] = "error"
                job["error"] = str(exc)
                job["updated_at"] = time.time()
        finally:
            try:
                in_path.unlink(missing_ok=True)
            except OSError:
                pass


@app.post("/api/jobs/instant")
def create_instant_job(
    file: UploadFile = File(...),
    fps: int = Form(120),
    slow_mo: str = Form("false"),
):
    fps_val = 120 if int(fps) >= 90 else 60
    is_slow = str(slow_mo).strip().lower() in ("1", "true", "yes", "on")
    job_id = uuid.uuid4().hex[:10]
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    in_path = job_dir / f"input{ext}"
    save_upload_to_disk(file, in_path)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": "instant",
            "status": "queued",
            "progress": 2,
            "stage": "Queued",
            "job_dir": str(job_dir),
            "output_path": None,
            "output_name": None,
            "output_size": 0,
            "elapsed_s": 0.0,
            "error": None,
            "logs_tail": [],
            "proc": None,
            "cancelled": False,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    t = threading.Thread(
        target=_run_instant_worker,
        args=(job_id, in_path, job_dir, fps_val, is_slow),
        daemon=True,
    )
    t.start()
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/jobs/blur")
def create_blur_job(
    file: UploadFile = File(...),
    config_json: str = Form("{}"),
):
    try:
        cfg = json.loads(config_json or "{}")
    except json.JSONDecodeError:
        cfg = {}

    job_id = uuid.uuid4().hex[:10]
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    in_path = job_dir / f"input{ext}"
    save_upload_to_disk(file, in_path)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": "blur",
            "status": "queued",
            "progress": 2,
            "stage": "Queued",
            "job_dir": str(job_dir),
            "output_path": None,
            "output_name": None,
            "output_size": 0,
            "elapsed_s": 0.0,
            "error": None,
            "logs_tail": [],
            "proc": None,
            "cancelled": False,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    t = threading.Thread(
        target=_run_blur_worker,
        args=(job_id, in_path, job_dir, cfg),
        daemon=True,
    )
    t.start()
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "id": job["id"],
            "status": job["status"],
            "progress": job["progress"],
            "stage": job["stage"],
            "output_size": job["output_size"],
            "output_name": job["output_name"],
            "elapsed_s": job["elapsed_s"],
            "error": job["error"],
            "logs_tail": list(job["logs_tail"]),
        }


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job["status"] != "done" or not job["output_path"]:
            raise HTTPException(status_code=404, detail="Output not ready")
        out_path = Path(job["output_path"])
        out_name = job["output_name"] or "AbuAsrh.mp4"
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Output expired")
    media_type = "video/x-matroska" if out_name.endswith(".mkv") else "video/mp4"
    return FileResponse(str(out_path), media_type=media_type, filename=out_name)


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        job["cancelled"] = True
        proc: Optional[subprocess.Popen] = job.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        job["status"] = "cancelled"
        job["updated_at"] = time.time()
        job_dir = Path(job["job_dir"])
    shutil.rmtree(job_dir, ignore_errors=True)
    return {"ok": True, "status": "cancelled"}


app.mount("/", StaticFiles(directory=str(BASE_DIR), html=True), name="static")
