import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from faster_whisper import WhisperModel

# Que el registro de GitHub muestre cada linea al instante
sys.stdout.reconfigure(line_buffering=True)

CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

# Nombres de usuario de Twitch (lo que va en twitch.tv/NOMBRE)
CANALES = ["optical", "byking", "locolucas", "lujo", "goti", "elmariana", "coscu", "davooxeneize", "momo", "lolitofdez", "spreen", "nicocapo", "bananirou", "overtflow", "ibai", "auronplay", "thegrefg", "elxokas", "knekro", "elmillor", "illojuan", "rubius", "ampeterby7", "tazercraft", "komanche", "carreraaa"]
MAX_CLIPS_POR_CANAL = 1   # clips que se piden por canal al revisar
MAX_CLIPS_TOTAL = 6       # clips que realmente se procesan (los mas vistos)
DIAS = 30
RECORTE_ABAJO = 0.10      # % del borde inferior que se corta para tapar subtitulos del propio clip (0 = no cortar)
MODELO_WHISPER = "small"  # mas preciso que "base"

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
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def api(path, params, token):
    r = requests.get(
        f"https://api.twitch.tv/helix/{path}",
        params=params,
        headers={"Client-Id": CLIENT_ID, "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]


def srt_time(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def procesar_clip(c, model, nombre):
    cid = c["id"]
    raw = TMP / f"{cid}.mp4"
    srt = TMP / f"{cid}.srt"
    final = OUT / f"{nombre}_{cid}.mp4"

    # 1. descargar
    subprocess.run(["yt-dlp", "-q", "-o", str(raw), c["url"]], check=True, timeout=180)

    # 2. subtitulos (IA que transcribe el audio)
    segments, _ = model.transcribe(
        str(raw),
        language="es",
        vad_filter=True,                    # ignora silencios y ruido (evita texto inventado)
        condition_on_previous_text=False,   # evita que repita lo anterior
        beam_size=5,
    )
    limpios = []
    for s in segments:
        texto = s.text.strip()
        if not texto:
            continue
        # saltar lineas repetidas seguidas
        if limpios and texto.lower() == limpios[-1][2].lower():
            limpios[-1][1] = s.end
            continue
        limpios.append([s.start, s.end, texto])
    # que nunca se solapen dos subtitulos
    for i in range(len(limpios) - 1):
        if limpios[i][1] > limpios[i + 1][0]:
            limpios[i][1] = limpios[i + 1][0]
    with open(srt, "w", encoding="utf-8") as f:
        for i, (ini, fin, texto) in enumerate(limpios, 1):
            f.write(f"{i}\n{srt_time(ini)} --> {srt_time(fin)}\n{texto}\n\n")

    # 3. vertical 9:16 + subtitulos, todo en una sola pasada (mas rapido)
    vf = (
        f"[0:v]crop=iw:trunc(ih*{1 - RECORTE_ABAJO}/2)*2:0:0,split[a][b];"
        "[a]scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280,boxblur=20:5[bg];"
        "[b]scale=720:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[v];"
        f"[v]subtitles={srt}:force_style='Alignment=2,FontSize=14,MarginV=140'[out]"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
         "-filter_complex", vf, "-map", "[out]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-c:a", "copy", str(final)],
        check=True, timeout=300,
    )

    # 4. titulo y descripcion con credito
    titulo = f"{c['title']} | {nombre}"
    desc = (
        f"Clip original: {c['url']}\n"
        f"Clip por {c['creator_name']} en Twitch\n"
        f"Canal: twitch.tv/{nombre}"
    )
    (OUT / f"{nombre}_{cid}.txt").write_text(f"{titulo}\n\n{desc}", encoding="utf-8")
    print("Listo:", final)


def main():
    token = get_token()
    desde = (datetime.utcnow() - timedelta(days=DIAS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # PASO 1: revisar todos los canales (rapido, solo consultas)
    print("=== REVISANDO CANALES ===")
    candidatos = []
    for canal in CANALES:
        try:
            users = api("users", {"login": canal}, token)
            if not users:
                print(f"[{canal}] NO EXISTE en Twitch con ese nombre")
                continue
            clips = api(
                "clips",
                {"broadcaster_id": users[0]["id"], "started_at": desde,
                 "first": MAX_CLIPS_POR_CANAL},
                token,
            )
        except Exception as e:
            print(f"[{canal}] error al consultar: {e}")
            continue
        print(f"[{canal}] existe, clips encontrados: {len(clips)}")
        for c in clips:
            candidatos.append((canal, c))

    print(f"\nCanales con clips: {len(set(n for n, _ in candidatos))} de {len(CANALES)}")
    if not candidatos:
        print("No hay clips para procesar.")
        return

    # PASO 2: procesar solo los mas vistos
    candidatos.sort(key=lambda x: x[1].get("view_count", 0), reverse=True)
    elegidos = candidatos[:MAX_CLIPS_TOTAL]
    print(f"\n=== PROCESANDO {len(elegidos)} CLIPS ===")

    model = WhisperModel(MODELO_WHISPER, device="cpu", compute_type="int8")
    total = 0
    for canal, c in elegidos:
        try:
            print(f"[{canal}] procesando: {c['title']}")
            procesar_clip(c, model, canal)
            total += 1
        except Exception as e:
            print(f"[{canal}] fallo el clip {c['id']}: {e}")

    print(f"\nTotal de clips listos: {total}")


if __name__ == "__main__":
    main()
