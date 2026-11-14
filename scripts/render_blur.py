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


def probe_video(video_path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate,avg_frame_rate,color_range,color_space,color_transfer,color_primaries,pix_fmt,width,height",
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

    fr = stream.get("r_frame_rate") or stream.get("avg_frame_rate") or "60/1"
    if "/" in fr:
        num, den = fr.split("/", 1)
        fps_num, fps_den = int(num), int(den)
    else:
        fps_num, fps_den = int(float(fr)), 1

    return {
        "fps_num": fps_num,
        "fps_den": fps_den,
        "width": int(stream.get("width") or 1280),
        "height": int(stream.get("height") or 720),
        "color_range": stream.get("color_range"),
        "color_space": stream.get("color_space"),
        "color_transfer": stream.get("color_transfer"),
        "color_primaries": stream.get("color_primaries"),
        "pix_fmt": stream.get("pix_fmt") or "yuv420p",
        "sample_rate": int(a_stream.get("sample_rate") or 48000),
        "has_audio": bool(a_stream),
    }


def load_plugins_wrapper(blur_env: Path, width: int, height: int) -> Path:
    """Create a thin wrapper script that preloads VapourSynth plugins from blur_env/plugins,
    provides a rawvideo source fallback if bestsource cannot load due to system FFmpeg ABI,
    and executes f0e/blur's official blur.py."""
    wrapper = blur_env / "scripts" / "run_blur_wrapper.py"
    code = f"""import vapoursynth as vs
from vapoursynth import core
from pathlib import Path
import runpy, sys

plugin_dir = Path({repr(str(blur_env / "plugins"))})
ignored = {{"libbestsource.so"}}
for plugin in sorted(plugin_dir.glob("*.so")):
    if plugin.name not in ignored:
        try:
            core.std.LoadPlugin(path=str(plugin))
            print("Loaded plugin:", plugin.name, file=sys.stderr)
        except Exception as e:
            print("Plugin load warning:", plugin.name, e, file=sys.stderr)

# If bestsource failed to load (e.g. libavutil.so.60 ABI mismatch on Ubuntu 24.04),
# fall back to FFmpeg-decoded raw YUV420P8 clip in VapourSynth.
if not hasattr(core, "bs"):
    import subprocess as _sp, numpy as _np
    class _RawBSFallback:
        @staticmethod
        def VideoSource(source, cachemode=0, fpsnum=None, fpsden=None):
            w, h = {width}, {height}
            fn = int(fpsnum or 60)
            fd = int(fpsden or 1)
            raw_cmd = ["ffmpeg", "-v", "error", "-i", str(source), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
            raw_bytes = _sp.check_output(raw_cmd)
            frame_sz = (w * h * 3) // 2
            n_frames = len(raw_bytes) // frame_sz
            blank = core.std.BlankClip(width=w, height=h, format=vs.YUV420P8, length=n_frames, fpsnum=fn, fpsden=fd)
            mv = memoryview(raw_bytes)
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
        # Replace deduplicate svp -> old (mvtools) and interpolation svp -> mvtools (commented out in official blur.py)
        src = src.replace('        case "svp":\n            video = blur.deduplicate.fill_drops_multiple(', '        case "svp":\n            video = blur.deduplicate.fill_drops_mvtools(video, threshold=deduplicate_threshold, debug=settings["debug"])\n        case "_svp_disabled":\n            video = blur.deduplicate.fill_drops_multiple(')
        src = src.replace('            case _:  # svp', '            case _:  # mvtools_fallback\n                video = blur.interpolate.interpolate_mvtools(video, int(interpolated_fps), blocksize=interpolation_blocksize, masking=interpolation_mask_area)\n            case "_svp_disabled":')
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

    wrapper = load_plugins_wrapper(blur_env, info["width"], info["height"])

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

    # Color metadata preservation matching f0e/blur rendering.cpp
    setparams = []
    if info.get("color_range"):
        setparams.append("range=" + ("full" if info["color_range"] == "pc" else "limited"))
    if info.get("color_space") and info["color_space"] != "unknown":
        setparams.append("colorspace=" + info["color_space"])
    if info.get("color_transfer") and info["color_transfer"] != "unknown":
        setparams.append("color_trc=" + info["color_transfer"])
    if info.get("color_primaries") and info["color_primaries"] != "unknown":
        setparams.append("color_primaries=" + info["color_primaries"])
    if setparams:
        ff_cmd += ["-vf", "setparams=" + ":".join(setparams)]

    if info.get("pix_fmt"):
        ff_cmd += ["-pix_fmt", info["pix_fmt"]]

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
        presets = {
            "h264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(q)],
            "h265": ["-c:v", "libx265", "-preset", "veryfast", "-crf", str(q)],
            "vp9": ["-c:v", "libvpx-vp9", "-deadline", "good", "-crf", str(q), "-b:v", "0"],
            "av1": ["-c:v", "libaom-av1", "-cpu-used", "4", "-crf", str(q)],
        }
        ff_cmd += presets.get(preset_name, presets["h264"])
        ff_cmd += ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"]

    ff_cmd.append(str(out_file))

    print("Running vspipe -> ffmpeg...")
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
