#!/usr/bin/env python3
"""
Telegram Bot for f0e/blur Cloud Rendering via GitHub Actions.
Runs on GitHub Actions (free cron/long-polling) or locally — zero impact on USxz trading server.
Receives video/document or URL + optional config overrides, dispatches render_blur.yml,
and sends rendered video back to the user on Telegram.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Optional

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"].strip()
GH_TOKEN = os.environ["GH_TOKEN"].strip()
GH_REPO = os.environ.get("GH_REPO", "rtyupp/flowtik-web").strip()
ALLOWED_USERS = {
    x.strip() for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()
}

USER_CONFIGS: dict[str, dict] = {}

DEFAULT_CFG = {
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
    "video_container": "mp4",
    "blur_weighting_gaussian_std_dev": 1.0,
    "blur_weighting_gaussian_mean": 2.0,
    "blur_weighting_gaussian_bound": "[0,2]",
    "svp_interpolation_preset": "weak",
    "svp_interpolation_algorithm": "13",
    "interpolation_blocksize": "8",
    "interpolation_mask_area": 0,
    "rife_model": "rife-v4.26_ensembleFalse",
}


def tg_api(method: str, payload: Optional[dict] = None) -> dict:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.loads(resp.read().decode("utf-8"))


def dispatch_render(video_ref: str, chat_id: str, cfg: dict) -> str:
    job_id = uuid.uuid4().hex[:8]
    cfg_b64 = base64.b64encode(json.dumps(cfg).encode("utf-8")).decode("ascii")
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/render_blur.yml/dispatches"
    body = json.dumps(
        {
            "ref": "main",
            "inputs": {
                "video_url": video_ref,
                "blur_config_b64": cfg_b64,
                "tg_chat_id": str(chat_id),
                "job_id": job_id,
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GH_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"GitHub HTTP {resp.status}")
    return job_id


def apply_kv_overrides(cfg: dict, text: str) -> dict:
    out = dict(cfg)
    for line in text.replace(";", "\n").splitlines():
        if "=" not in line and ":" not in line:
            continue
        sep = "=" if "=" in line else ":"
        k, v = [p.strip() for p in line.split(sep, 1)]
        k = k.lower().replace(" ", "_")
        if k not in out:
            continue
        cur = out[k]
        if isinstance(cur, bool):
            out[k] = v.lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            out[k] = int(float(v))
        elif isinstance(cur, float):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def handle_message(msg: dict):
    chat_id = str(msg["chat"]["id"])
    if ALLOWED_USERS and chat_id not in ALLOWED_USERS:
        return
    cfg = USER_CONFIGS.setdefault(chat_id, dict(DEFAULT_CFG))
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if text == "/start" or text == "/help":
        tg_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    "🌊 بوت بلور (f0e/blur الرسمي)\n\n"
                    "أرسل أي فيديو (حتى 20MB مباشرة) أو رابط فيديو مباشر، وسيتم رندره بمحرك VapourSynth + SVP/RIFE وإرساله لك هنا.\n\n"
                    "لتغيير الإعدادات أرسل:\n"
                    "`blur_amount=1.2`\n"
                    "`blur_weighting=gaussian_sym`\n"
                    "`interpolated_fps=600`\n"
                    "`blur_output_fps=60`\n"
                    "`interpolation_method=svp` (أو `rife`)\n"
                    "`quality=16`\n\n"
                    "/cfg لعرض إعداداتك الحالية\n"
                    "/reset لإرجاع الافتراضي"
                ),
                "parse_mode": "Markdown",
            },
        )
        return

    if text == "/cfg":
        tg_api(
            "sendMessage",
            {"chat_id": chat_id, "text": json.dumps(cfg, indent=2, ensure_ascii=False)},
        )
        return

    if text == "/reset":
        USER_CONFIGS[chat_id] = dict(DEFAULT_CFG)
        tg_api("sendMessage", {"chat_id": chat_id, "text": "✅ تم تصفير الإعدادات للافتراضي."})
        return

    if text and ("=" in text or ":" in text) and not text.startswith("http"):
        cfg = apply_kv_overrides(cfg, text)
        USER_CONFIGS[chat_id] = cfg
        if not msg.get("video") and not msg.get("document"):
            tg_api(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": f"✅ تم تحديث الإعدادات:\nblur_amount={cfg['blur_amount']} | {cfg['blur_weighting']} | {cfg['interpolated_fps']} -> {cfg['blur_output_fps']}fps | {cfg['interpolation_method']}",
                },
            )
            return

    video_ref = None
    if msg.get("video"):
        video_ref = "tg:" + msg["video"]["file_id"]
    elif msg.get("document") and str(msg["document"].get("mime_type", "")).startswith("video/"):
        video_ref = "tg:" + msg["document"]["file_id"]
    elif text.startswith("http://") or text.startswith("https://"):
        video_ref = text.split()[0]

    if not video_ref:
        return

    try:
        job_id = dispatch_render(video_ref, chat_id, cfg)
        tg_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"🚀 بدأ الرندر على محرك f0e/blur (Job `{job_id}`)\n"
                    f"• القوة: {cfg['blur_amount']} | الوزن: {cfg['blur_weighting']}\n"
                    f"• التوليد: {cfg['interpolated_fps']} ({cfg['interpolation_method'].upper()}) ← المخرج: {cfg['blur_output_fps']}fps\n"
                    "بيوصلك الفيديو هنا أول ما يخلص (عادة 1–4 دقائق)."
                ),
                "parse_mode": "Markdown",
            },
        )
    except Exception as e:
        tg_api("sendMessage", {"chat_id": chat_id, "text": f"❌ تعذر تشغيل الرندر: {e}"})


def main():
    offset = 0
    end_at = time.time() + int(os.environ.get("RUN_SECONDS", "300"))
    print("Blur Telegram Bot polling started...")
    while time.time() < end_at:
        try:
            res = tg_api("getUpdates", {"offset": offset, "timeout": 25})
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    handle_message(upd["message"])
        except Exception as e:
            print("Poll error:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
