import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import requests
from faster_whisper import WhisperModel

CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
CANAL = "westcol"
MAX_CLIPS = 3

OUT = Path("output")
TMP = Path("tmp")
OUT.mkdir(exist_ok=True)
TMP.mkdir(exist_ok=True)


def get_token():
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


def api(path, params, token):
    r = requests.get(
        f"https://api.twitch.tv/helix/{path}",
        params=params,
        headers={"Client-Id": CLIENT_ID, "Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json()["data"]


def srt_time(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def main():
    token = get_token()
    user = api("users", {"login": CANAL}, token)[0]
    desde = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    clips = api(
        "clips",
        {"broadcaster_id": user["id"], "started_at": desde, "first": MAX_CLIPS},
        token,
    )
    if not clips:
        print("No hay clips nuevos hoy.")
        return

    model = WhisperModel("base", device="cpu", compute_type="int8")

    for c in clips:
        cid = c["id"]
        raw = TMP / f"{cid}.mp4"
        vertical = TMP / f"{cid}_v.mp4"
        srt = TMP / f"{cid}.srt"
        final = OUT / f"{cid}.mp4"

        # 1. descargar
        subprocess.run(["yt-dlp", "-o", str(raw), c["url"]], check=True)

        # 2. pasar a vertical 9:16 con fondo difuminado
        vf = (
            "[0:v]split[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:5[bg];"
            "[b]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw), "-filter_complex", vf,
             "-c:a", "copy", str(vertical)],
            check=True,
        )

        # 3. subtitulos
        segments, _ = model.transcribe(str(raw), language="es")
        with open(srt, "w", encoding="utf-8") as f:
            for i, s in enumerate(segments, 1):
                f.write(f"{i}\n{srt_time(s.start)} --> {srt_time(s.end)}\n{s.text.strip()}\n\n")

        subprocess.run(
            ["ffmpeg", "-y", "-i", str(vertical),
             "-vf", f"subtitles={srt}:force_style='Alignment=2,FontSize=16,MarginV=200'",
             "-c:a", "copy", str(final)],
            check=True,
        )

        # 4. titulo y descripcion con credito
        titulo = f"{c['title']} | Westcol"
        desc = f"Clip original: {c['url']}\nClip por {c['creator_name']} en Twitch\nCanal: twitch.tv/{CANAL}"
        (OUT / f"{cid}.txt").write_text(f"{titulo}\n\n{desc}", encoding="utf-8")
        print("Listo:", final)


if __name__ == "__main__":
    main()
