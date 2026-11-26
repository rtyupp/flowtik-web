#!/usr/bin/env python3
"""
Headless runner for the official f0e/blur VapourSynth pipeline.
Builds the exact JSON settings object consumed by f0e/blur's blur.py,
pipes vspipe -> ffmpeg with full color metadata, timescale audio filters,
and preset encoding.
"""
import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


DEFAULT_SETTINGS = {
    "blur": True,
    "blur_amount": 1.0,
    "blur_output_fps": 60,
    "blur_weighting": "gaussian_sym",
    "blur_gamma": 1.0,
    "interpolate": True,
    "interpolated_fps": "600",
    "interpolation_method": "svp",
    "pre_interpolate": False,
    "pre_interpolated_fps": "360",
    "deduplicate": True,
    "deduplicate_method": "svp",
    "deduplicate_range": 2,
    "deduplicate_threshold": "0.001",
    "timescale": False,
    "input_timescale": 1.0,
    "output_timescale": 1.0,
    "output_timescale_audio_pitch": False,
    "filters": False,
    "brightness": 1.0,
    "saturation": 1.0,
    "contrast": 1.0,
    "encode_preset": "h264",
    "quality": 16,
    "gpu_decoding": False,
    "gpu_interpolation": False,
    "gpu_encoding": False,
    "video_container": "mp4",
    "ffmpeg_override": "",
    "debug": False,
    "blur_weighting_gaussian_std_dev": 1.0,
    "blur_weighting_gaussian_mean": 2.0,
    "blur_weighting_gaussian_bound": "[0,2]",
    "svp_interpolation_preset": "weak",
    "svp_interpolation_algorithm": "13",
    "interpolation_blocksize": "8",
    "interpolation_mask_area": 0,
    "rife_model": "rife-v4.26_ensembleFalse",
    "rife_gpu_index": 0,
    "manual_svp": False,
    "super_string": "",
    "vectors_string": "",
    "smooth_string": "",
}


def _parse_frac(rate_str: str) -> tuple[int, int, float]:
    s = (rate_str or "").strip()
    if not s:
        return 0, 1, 0.0
    try:
        if "/" in s:
            num_s, den_s = s.split("/", 1)
            num, den = int(num_s), int(den_s)
            if den <= 0 or num <= 0:
                return 0, 1, 0.0
            return num, den, num / den
        val = float(s)
        if val <= 0:
            return 0, 1, 0.0
        num = int(round(val * 1000))
        return num, 1000, val
    except (ValueError, ZeroDivisionError):
        return 0, 1, 0.0


def probe_video(video_path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate,avg_frame_rate,color_range,color_space,color_transfer,color_primaries,pix_fmt,width,height:stream_side_data=rotation",
        "-of",
        "json",
        str(video_path),
    ]
    v_info = json.loads(subprocess.check_output(cmd, text=True))
    stream = (v_info.get("streams") or [{}])[0]

    a_cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate",
        "-of",
        "json",
        str(video_path),
    ]
    a_info = json.loads(subprocess.check_output(a_cmd, text=True))
    a_stream = (a_info.get("streams") or [{}])[0]

    r_num, r_den, r_val = _parse_frac(stream.get("r_frame_rate") or "")
    a_num, a_den, a_val = _parse_frac(stream.get("avg_frame_rate") or "")

    r_valid = 1.0 <= r_val <= 240.0
    a_valid = 1.0 <= a_val <= 240.0
    if a_valid and (not r_valid or abs(a_val - r_val) / max(r_val, 1e-6) > 0.02):
        fps_num, fps_den = a_num, a_den
    elif r_valid:
        fps_num, fps_den = r_num, r_den
    elif a_valid:
        fps_num, fps_den = a_num, a_den
    else:
        fps_num, fps_den = 60, 1

    w = int(stream.get("width") or 1280)
    h = int(stream.get("height") or 720)
    for sd in stream.get("side_data_list") or []:
        rot = sd.get("rotation")
        if rot is not None:
            try:
                if abs(int(round(float(rot)))) % 180 == 90:
                    w, h = h, w
            except (TypeError, ValueError):
                pass
    w = max(2, w - (w % 2))
    h = max(2, h - (h % 2))

    needs_scale = False
    if max(w, h) > 1920:
        needs_scale = True
        if w >= h:
            target_w = 1920
            target_h = int(round(h * (1920.0 / w)))
        else:
            target_h = 1920
            target_w = int(round(w * (1920.0 / h)))
        w = max(2, target_w - (target_w % 2))
        h = max(2, target_h - (target_h % 2))

    return {
        "fps_num": fps_num,
        "fps_den": fps_den,
        "width": w,
        "height": h,
        "needs_scale": needs_scale,
        "color_range": stream.get("color_range"),
        "color_space": stream.get("color_space"),
        "color_transfer": stream.get("color_transfer"),
        "color_primaries": stream.get("color_primaries"),
        "pix_fmt": stream.get("pix_fmt") or "yuv420p",
        "sample_rate": int(a_stream.get("sample_rate") or 48000),
        "has_audio": bool(a_stream),
    }


def load_plugins_wrapper(blur_env: Path, width: int, height: int, needs_scale: bool = False) -> Path:
    """Create a thin wrapper script that preloads VapourSynth plugins from blur_env/plugins,
    provides a rawvideo source fallback if bestsource cannot load due to system FFmpeg ABI,
    and executes f0e/blur's official blur.py."""
    wrapper = blur_env / "scripts" / "run_blur_wrapper.py"
    scale_expr = f'["scale={width}:{height}:flags=lanczos"]' if needs_scale else "[]"
    code = f"""import vapoursynth as vs
from vapoursynth import core
from pathlib import Path
import runpy, sys

sys.stdout = sys.stderr

plugin_dir = Path({repr(str(blur_env / "plugins"))})
ignored = {{"libbestsource.so", "bestsource.so"}}
for plugin in sorted(plugin_dir.glob("*.so")):
    if plugin.name not in ignored:
        try:
            core.std.LoadPlugin(path=str(plugin))
            print("Loaded plugin:", plugin.name, file=sys.stderr)
        except Exception as e:
            print("Plugin load warning:", plugin.name, e, file=sys.stderr)

# If bestsource failed to load (e.g. libavutil.so.60 ABI mismatch on Ubuntu 24.04),
# fall back to FFmpeg-decoded raw YUV420P8 clip streamed to disk and mmap'd in VapourSynth.
if not hasattr(core, "bs"):
    import atexit as _atexit, mmap as _mmap, os as _os, subprocess as _sp, tempfile as _tf, numpy as _np
    class _RawBSFallback:
        @staticmethod
        def VideoSource(source, cachemode=0, fpsnum=None, fpsden=None):
            w, h = {width}, {height}
            fn = int(fpsnum or 60)
            fd = int(fpsden or 1)
            tmp_fh = _tf.NamedTemporaryFile(prefix="blur_raw_", suffix=".yuv", delete=False)
            tmp_path = tmp_fh.name
            tmp_fh.close()
            vf_parts = {scale_expr}
            raw_cmd = [
                "ffmpeg", "-v", "error", "-y", "-i", str(source),
                *(["-vf", ",".join(vf_parts)] if vf_parts else []),
                "-fps_mode", "cfr", "-r", f"{{fn}}/{{fd}}",
                "-f", "rawvideo", "-pix_fmt", "yuv420p", tmp_path,
            ]
            _sp.check_call(raw_cmd)
            frame_sz = (w * h * 3) // 2
            file_sz = _os.path.getsize(tmp_path)
            n_frames = file_sz // frame_sz if frame_sz > 0 else 0
            if n_frames <= 0:
                try:
                    _os.unlink(tmp_path)
                except OSError:
                    pass
                raise RuntimeError("Rawvideo fallback decoded 0 frames")
            fh = open(tmp_path, "rb")
            mm = _mmap.mmap(fh.fileno(), 0, access=_mmap.ACCESS_READ)
            mv = memoryview(mm)
            def _cleanup():
                try:
                    mv.release()
                except Exception:
                    pass
                try:
                    mm.close()
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass
                try:
                    if _os.path.exists(tmp_path):
                        _os.unlink(tmp_path)
                except OSError:
                    pass
            _atexit.register(_cleanup)
            blank = core.std.BlankClip(width=w, height=h, format=vs.YUV420P8, length=n_frames, fpsnum=fn, fpsden=fd)
            y_sz = w * h
            uv_sz = (w // 2) * (h // 2)
            def _fill(n, f):
                fout = f.copy()
                off = n * frame_sz
                _np.copyto(_np.asarray(fout[0]), _np.frombuffer(mv[off:off+y_sz], dtype=_np.uint8).reshape((h, w)))
                _np.copyto(_np.asarray(fout[1]), _np.frombuffer(mv[off+y_sz:off+y_sz+uv_sz], dtype=_np.uint8).reshape((h//2, w//2)))
                _np.copyto(_np.asarray(fout[2]), _np.frombuffer(mv[off+y_sz+uv_sz:off+frame_sz], dtype=_np.uint8).reshape((h//2, w//2)))
                return fout
            return core.std.ModifyFrame(blank, blank, _fill)
    _bs_fb = _RawBSFallback()
    blur_py_path = Path({repr(str(blur_env / "scripts" / "blur.py"))})
    src = blur_py_path.read_text(encoding="utf-8")
    if not hasattr(core, "bs"):
        src = src.replace("core.bs.VideoSource(", "_bs_fb.VideoSource(")
    if not hasattr(core, "svp1") or not hasattr(core, "svp2"):
        print("SVP API3 unavailable in this VapourSynth build; using MVTools/RIFE fallback.", file=sys.stderr)
        src = src.replace(
            '        case "svp":\\n            video = blur.deduplicate.fill_drops_multiple(',
            '        case "svp":\\n            video = blur.deduplicate.fill_drops_mvtools(video, threshold=deduplicate_threshold, debug=settings["debug"])\\n        case "_svp_disabled":\\n            video = blur.deduplicate.fill_drops_multiple('
        )
        src = src.replace(
            '            case _:  # svp',
            '            case "svp":\\n                video = blur.interpolate.interpolate_mvtools(video, int(round(float(interpolated_fps))), blocksize=interpolation_blocksize, masking=interpolation_mask_area)\\n            case _:  # svp_disabled'
        )
    globals_dict = globals().copy()
    globals_dict["__file__"] = str(blur_py_path)
    globals_dict["_bs_fb"] = _bs_fb
    exec(compile(src, str(blur_py_path), "exec"), globals_dict)
else:
    globals_dict = globals().copy()
    runpy.run_path({repr(str(blur_env / "scripts" / "blur.py"))}, init_globals=globals_dict)
"""
    wrapper.write_text(code, encoding="utf-8")
    return wrapper


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--blur-env", required=True)
    ap.add_argument("--config-b64", default="")
    ap.add_argument("--job-id", default="")
    args = ap.parse_args()

    input_path = Path(args.input).resolve()
    out_dir = Path(args.output_dir).resolve()
    blur_env = Path(args.blur_env).resolve()

    cfg = dict(DEFAULT_SETTINGS)
    if args.config_b64.strip():
        user_cfg = json.loads(base64.b64decode(args.config_b64.strip()).decode("utf-8"))
        for k, v in user_cfg.items():
            if k in cfg:
                cfg[k] = v

    # Resolve rife_model path inside blur_env/models
    rife_model_name = str(cfg.get("rife_model") or "rife-v4.26_ensembleFalse")
    rife_model_path = blur_env / "models" / rife_model_name
    cfg["rife_model"] = str(rife_model_path)

    # Ubuntu GitHub runners are CPU-only (no hardware GPU Vulkan/NVENC), so keep GPU flags safe:
    # Check if Vulkan ICD has a real GPU; if only llvmpipe, force svp CPU & cpu encoding
    if cfg.get("interpolation_method") == "rife" and not os.path.exists("/dev/nvidia0"):
        print("Note: CPU runner detected — RIFE/SVP will run without NVIDIA GPU")
    cfg["gpu_decoding"] = False
    cfg["gpu_interpolation"] = False
    cfg["gpu_encoding"] = False

    # Normalize bound string
    bound = str(cfg.get("blur_weighting_gaussian_bound", "[0,2]")).strip()
    if not bound.startswith("["):
        bound = f"[{bound}]"
    cfg["blur_weighting_gaussian_bound"] = bound

    # Ensure integer types where f0e/blur expects int/float
    cfg["blur"] = bool(cfg["blur"])
    cfg["blur_amount"] = float(cfg["blur_amount"])
    cfg["blur_output_fps"] = int(cfg["blur_output_fps"])
    cfg["blur_gamma"] = float(cfg["blur_gamma"])
    cfg["interpolate"] = bool(cfg["interpolate"])
    cfg["interpolated_fps"] = str(cfg["interpolated_fps"])
    cfg["pre_interpolate"] = bool(cfg["pre_interpolate"])
    cfg["pre_interpolated_fps"] = str(cfg["pre_interpolated_fps"])
    cfg["deduplicate"] = bool(cfg["deduplicate"])
    cfg["deduplicate_range"] = int(cfg["deduplicate_range"])
    cfg["deduplicate_threshold"] = str(cfg["deduplicate_threshold"])
    cfg["timescale"] = bool(cfg["timescale"])
    cfg["input_timescale"] = float(cfg["input_timescale"])
    cfg["output_timescale"] = float(cfg["output_timescale"])
    cfg["output_timescale_audio_pitch"] = bool(cfg["output_timescale_audio_pitch"])
    cfg["filters"] = bool(cfg["filters"])
    cfg["brightness"] = float(cfg["brightness"])
    cfg["saturation"] = float(cfg["saturation"])
    cfg["contrast"] = float(cfg["contrast"])
    cfg["quality"] = int(cfg["quality"])
    cfg["blur_weighting_gaussian_std_dev"] = float(cfg["blur_weighting_gaussian_std_dev"])
    cfg["blur_weighting_gaussian_mean"] = float(cfg["blur_weighting_gaussian_mean"])
    cfg["interpolation_mask_area"] = int(cfg["interpolation_mask_area"])
    cfg["manual_svp"] = bool(cfg["manual_svp"])
    cfg["debug"] = bool(cfg["debug"])

    info = probe_video(input_path)
    print("Input video info:", info)
    print("Effective f0e/blur settings:", json.dumps(cfg, ensure_ascii=False))

    wrapper = load_plugins_wrapper(blur_env, info["width"], info["height"], info.get("needs_scale", False))

    import shutil, importlib.util
    vspipe_bin = shutil.which("vspipe")
    if not vspipe_bin:
        spec = importlib.util.find_spec("vapoursynth")
        if spec and spec.origin:
            cand = Path(spec.origin).resolve().parent / "vspipe"
            if cand.exists():
                vspipe_bin = str(cand)
        if not vspipe_bin:
            vspipe_bin = "vspipe"

    vspipe_cmd = [
        vspipe_bin,
        "-p",
        "-c",
        "y4m",
        "-a",
        f"video_path={input_path}",
        "-a",
        f"fps_num={info['fps_num']}",
        "-a",
        f"fps_den={info['fps_den']}",
        "-a",
        f"color_range={info['color_range'] or 'undefined'}",
        "-a",
        f"settings={json.dumps(cfg)}",
        str(wrapper),
        "-",
    ]

    container = str(cfg.get("video_container") or "mp4").lower()
    if container not in ("mp4", "mkv", "avi", "mov"):
        container = "mp4"
    out_file = out_dir / f"output_blur.{container}"

    ff_cmd = [
        "ffmpeg",
        "-loglevel",
        "warning",
        "-hide_banner",
        "-stats",
        "-y",
        "-f",
        "yuv4mpegpipe",
        "-i",
        "-",
        "-fflags",
        "+genpts",
        "-i",
        str(input_path),
        "-map",
        "0:v",
        "-map",
        "1:a?",
    ]

    in_fps = info["fps_num"] / max(1, info["fps_den"])
    if cfg["blur"]:
        out_fps = max(15, min(240, int(cfg["blur_output_fps"])))
    elif cfg["interpolate"]:
        raw_ifps = str(cfg.get("interpolated_fps") or "").strip().lower()
        try:
            if raw_ifps.endswith("x"):
                out_fps = int(round(in_fps * float(raw_ifps[:-1] or 2)))
            elif raw_ifps:
                out_fps = int(round(float(raw_ifps)))
            else:
                out_fps = int(cfg["blur_output_fps"])
        except ValueError:
            out_fps = int(cfg["blur_output_fps"])
        out_fps = max(15, min(240, out_fps))
    else:
        out_fps = max(15, min(240, int(round(in_fps))))

    vf_parts = [
        "setsar=1",
        "setparams=range=limited:colorspace=bt709:color_trc=bt709:color_primaries=bt709",
    ]
    ff_cmd += ["-vf", ",".join(vf_parts), "-pix_fmt", "yuv420p"]

    # Audio timescale filters matching f0e/blur rendering.cpp
    audio_filters = []
    if cfg["timescale"] and info["has_audio"]:
        sr = info["sample_rate"] or 48000
        if cfg["input_timescale"] != 1.0:
            audio_filters.append(f"asetrate={sr}*{1.0 / cfg['input_timescale']}")
            audio_filters.append("aresample=48000")
        if cfg["output_timescale"] != 1.0:
            if cfg["output_timescale_audio_pitch"]:
                audio_filters.append(f"asetrate={sr}*{cfg['output_timescale']}")
                audio_filters.append("aresample=48000")
            else:
                # Chain atempo if outside [0.5, 2.0]
                t = cfg["output_timescale"]
                while t > 2.0:
                    audio_filters.append("atempo=2.0")
                    t /= 2.0
                while t < 0.5:
                    audio_filters.append("atempo=0.5")
                    t /= 0.5
                audio_filters.append(f"atempo={t}")
    if audio_filters:
        ff_cmd += ["-af", ",".join(audio_filters)]

    if cfg.get("ffmpeg_override", "").strip():
        ff_cmd += shlex.split(cfg["ffmpeg_override"].strip())
    else:
        q = int(cfg["quality"])
        preset_name = str(cfg.get("encode_preset") or "h264").lower()
        level = "4.2" if out_fps <= 60 else "5.1"
        maxrate = "12M" if out_fps <= 60 else "18M"
        bufsize = "24M" if out_fps <= 60 else "36M"
        gop = str(out_fps * 2)
        keyint_min = str(out_fps)
        timescale = "120000" if out_fps > 60 else "60000"
        presets = {
            "h264": [
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-profile:v", "high",
                "-level:v", level,
                "-crf", str(q),
                "-maxrate", maxrate,
                "-bufsize", bufsize,
                "-g", gop,
                "-keyint_min", keyint_min,
                "-sc_threshold", "0",
                "-bf", "2",
                "-fps_mode", "cfr",
                "-r", str(out_fps),
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-colorspace", "bt709",
                "-color_range", "tv",
                "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709:open-gop=0:ref=3:bframes=2",
            ],
            "h265": ["-c:v", "libx265", "-preset", "veryfast", "-crf", str(q), "-fps_mode", "cfr", "-r", str(out_fps)],
            "vp9": ["-c:v", "libvpx-vp9", "-deadline", "good", "-crf", str(q), "-b:v", "0", "-fps_mode", "cfr", "-r", str(out_fps)],
            "av1": ["-c:v", "libaom-av1", "-cpu-used", "4", "-crf", str(q), "-fps_mode", "cfr", "-r", str(out_fps)],
        }
        ff_cmd += presets.get(preset_name, presets["h264"])
        if container in ("mp4", "mov"):
            ff_cmd += ["-video_track_timescale", timescale, "-brand", "mp42", "-movflags", "+faststart"]
        ff_cmd += [
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-metadata", "title=AbuAsrh",
            "-metadata", "artist=AbuAsrh",
            "-c:a", "aac",
            "-ar", "48000",
            "-ac", "2",
            "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            "-max_interleave_delta", "0",
        ]

    ff_cmd.append(str(out_file))

    print("Running vspipe -> ffmpeg...")
    sys.stdout.flush()
    sys.stderr.flush()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(blur_env / "scripts") + os.pathsep + env.get("PYTHONPATH", "")
    import importlib.util
    _vs_spec = importlib.util.find_spec("vapoursynth")
    vs_lib_dir = str(Path(_vs_spec.origin).resolve().parent) if (_vs_spec and _vs_spec.origin) else ""
    ld_parts = [str(blur_env / "plugins")]
    if vs_lib_dir:
        ld_parts.append(vs_lib_dir)
    if env.get("LD_LIBRARY_PATH"):
        ld_parts.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(ld_parts)

    p1 = subprocess.Popen(vspipe_cmd, stdout=subprocess.PIPE, env=env)
    p2 = subprocess.Popen(ff_cmd, stdin=p1.stdout)
    assert p1.stdout is not None
    p1.stdout.close()
    rc2 = p2.wait()
    rc1 = p1.wait()

    if rc1 != 0 or rc2 != 0:
        print(f"ERROR: vspipe exited {rc1}, ffmpeg exited {rc2}", file=sys.stderr)
        sys.exit(1)

    print("Render complete:", out_file, out_file.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
