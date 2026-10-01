import json
import os
import time
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from faster_whisper import WhisperModel

# Que el registro de GitHub muestre cada linea al instante
sys.stdout.reconfigure(line_buffering=True)

# ---------------- CLAVES (vienen de los secretos de GitHub) ----------------
CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "")
# Si faltan las claves de YouTube, el bot igual funciona: solo prepara los videos.
SUBIR_A_YOUTUBE = bool(YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN)

# ---------------- CONFIGURACION (lo que puedes cambiar) ----------------
# Nombres de usuario de Twitch (lo que va en twitch.tv/NOMBRE)
CANALES = ["optical", "byking", "locolucas", "lujo", "goti", "elmariana", "coscu", "davooxeneize", "momo", "lolitofdez", "spreen", "nicocapo", "bananirou", "overtflow", "ibai", "auronplay", "thegrefg", "elxokas", "knekro", "elmillor", "illojuan", "rubius", "ampeterby7", "tazercraft", "komanche", "carreraaa"]
MAX_CLIPS_POR_CANAL = 15   # maximo de clips por streamer cada dia
MAX_CLIPS_TOTAL = 100      # tope diario (YouTube deja ~100 subidas al dia por defecto)
MAX_MINUTOS = 60           # tiempo maximo de trabajo por corrida (GitHub gratis = 2000 min/mes)
DIAS = 1                   # mira clips de las ultimas X dias (1 = solo el ultimo dia)
DURACION_MINIMA = 8        # segundos: ignora clips mas cortos que esto
RECORTE_ABAJO = 0.10       # % del borde inferior que se corta (0 = no cortar)
MODELO_WHISPER = "small"   # "base" o "tiny" = mas rapido, "small" = mas preciso
PRIVACIDAD_YT = "private"  # "private" hasta que Google apruebe tu proyecto; luego "public"
CATEGORIA_YT = "24"        # 24 = Entretenimiento, 20 = Gaming

OUT = Path("output")
TMP = Path("tmp")
PROCESADOS = Path("procesados.txt")   # lista de clips ya subidos (para no repetir)
OUT.mkdir(exist_ok=True)
TMP.mkdir(exist_ok=True)
PROCESADOS.touch(exist_ok=True)


# ---------------- TWITCH ----------------
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


# ---------------- YOUTUBE ----------------
def token_youtube():
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": YT_CLIENT_ID,
            "client_secret": YT_CLIENT_SECRET,
            "refresh_token": YT_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def subir_a_youtube(ruta, titulo, descripcion, yt_token):
    meta = {
        "snippet": {
            "title": titulo[:100],
            "description": descripcion[:4900],
            "categoryId": CATEGORIA_YT,
        },
        "status": {
            "privacyStatus": PRIVACIDAD_YT,
            "selfDeclaredMadeForKids": False,
        },
    }
    tam = Path(ruta).stat().st_size
    ini = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {yt_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(tam),
        },
        data=json.dumps(meta),
        timeout=60,
    )
    ini.raise_for_status()
    url = ini.headers["Location"]
    with open(ruta, "rb") as f:
        sub = requests.put(
            url, headers={"Content-Type": "video/mp4"}, data=f, timeout=900
        )
    sub.raise_for_status()
    return sub.json()["id"]


# ---------------- VIDEO ----------------
def srt_time(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def limpiar_segmentos(segments):
    """Quita lineas vacias/repetidas y evita que se solapen."""
    limpios = []
    for s in segments:
        texto = s.text.strip()
        if not texto:
            continue
        if limpios and texto.lower() == limpios[-1][2].lower():
            limpios[-1][1] = s.end
            continue
        limpios.append([s.start, s.end, texto])
    for i in range(len(limpios) - 1):
        if limpios[i][1] > limpios[i + 1][0]:
            limpios[i][1] = limpios[i + 1][0]
    return limpios


def procesar_clip(c, model, nombre):
    cid = c["id"]
    raw = TMP / f"{cid}.mp4"
    srt = TMP / f"{cid}.srt"
    final = OUT / f"{nombre}_{cid}.mp4"

    # 1. descargar
    subprocess.run(["yt-dlp", "-q", "-o", str(raw), c["url"]], check=True, timeout=180)

    # 2. subtitulos
    segments, _ = model.transcribe(
        str(raw),
        language="es",
        vad_filter=True,
        condition_on_previous_text=False,
        beam_size=5,
    )
    limpios = limpiar_segmentos(segments)
    with open(srt, "w", encoding="utf-8") as f:
        for i, (ini, fin, texto) in enumerate(limpios, 1):
            f.write(f"{i}\n{srt_time(ini)} --> {srt_time(fin)}\n{texto}\n\n")

    # 3. vertical 9:16 + subtitulos en una sola pasada
    vf = (
        f"[0:v]crop=iw:trunc(ih*{1 - RECORTE_ABAJO}/2)*2:0:0,split[a][b];"
        "[a]scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280,boxblur=20:5[bg];"
        "[b]scale=720:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[v];"
        f"[v]subtitles={srt}:force_style='Alignment=2,FontSize=14,Outline=2,MarginV=45'[out]"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
         "-filter_complex", vf, "-map", "[out]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-c:a", "copy", str(final)],
        check=True, timeout=300,
    )

    # 4. titulo y descripcion con credito
    sufijo = " #Shorts"
    base = f"{c['title']} | {nombre}"
    titulo = base[: 100 - len(sufijo)] + sufijo
    desc = (
        f"Clip original: {c['url']}\n"
        f"Clip por {c['creator_name']} en Twitch\n"
        f"Canal: twitch.tv/{nombre}\n\n#Shorts"
    )
    (OUT / f"{nombre}_{cid}.txt").write_text(f"{titulo}\n\n{desc}", encoding="utf-8")
    return final, titulo, desc


def cargar_procesados():
    return set(x.strip() for x in PROCESADOS.read_text().splitlines() if x.strip())


def marcar_procesado(cid):
    with open(PROCESADOS, "a", encoding="utf-8") as f:
        f.write(cid + "\n")


# ---------------- PROGRAMA PRINCIPAL ----------------
def buscar_candidatos(token, ya):
    desde = (datetime.utcnow() - timedelta(days=DIAS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    candidatos = []
    print("=== REVISANDO CANALES ===")
    for canal in CANALES:
        try:
            users = api("users", {"login": canal}, token)
            if not users:
                print(f"[{canal}] NO EXISTE en Twitch con ese nombre")
                continue
            clips = api(
                "clips",
                {"broadcaster_id": users[0]["id"], "started_at": desde, "first": 100},
                token,
            )
        except Exception as e:
            print(f"[{canal}] error al consultar: {e}")
            continue
        utiles = [
            c for c in clips
            if c["id"] not in ya and c.get("duration", 0) >= DURACION_MINIMA
        ]
        utiles = utiles[:MAX_CLIPS_POR_CANAL]   # Twitch ya los ordena por vistas
        print(f"[{canal}] existe, clips nuevos utiles: {len(utiles)}")
        for c in utiles:
            candidatos.append((canal, c))
    return candidatos


def main():
    token = get_token()
    ya = cargar_procesados()
    candidatos = buscar_candidatos(token, ya)

    print(f"\nCanales con clips: {len(set(n for n, _ in candidatos))} de {len(CANALES)}")
    if not candidatos:
        print("No hay clips nuevos para procesar.")
        return

    candidatos.sort(key=lambda x: x[1].get("view_count", 0), reverse=True)
    elegidos = candidatos[:MAX_CLIPS_TOTAL]
    print(f"\n=== PROCESANDO {len(elegidos)} CLIPS (subir a YouTube: {SUBIR_A_YOUTUBE}) ===")

    model = WhisperModel(MODELO_WHISPER, device="cpu", compute_type="int8")
    yt_token = token_youtube() if SUBIR_A_YOUTUBE else None
    listos = subidos = 0

    inicio = time.time()
    for canal, c in elegidos:
        if (time.time() - inicio) / 60 > MAX_MINUTOS:
            print(f"Se acabo el tiempo ({MAX_MINUTOS} min). Lo que falta sigue manana.")
            break
        try:
            print(f"[{canal}] procesando: {c['title']}")
            final, titulo, desc = procesar_clip(c, model, canal)
            listos += 1
            print("Listo:", final)
        except Exception as e:
            print(f"[{canal}] fallo el clip {c['id']}: {e}")
            continue

        if SUBIR_A_YOUTUBE:
            try:
                vid = subir_a_youtube(final, titulo, desc, yt_token)
                marcar_procesado(c["id"])
                subidos += 1
                print(f"[{canal}] subido a YouTube: https://youtube.com/shorts/{vid}")
            except requests.HTTPError as e:
                texto = e.response.text if e.response is not None else str(e)
                print(f"[{canal}] fallo la subida: {texto[:300]}")
                if "quota" in texto.lower() or "uploadLimitExceeded" in texto:
                    print("Se llego al limite diario de YouTube. Paro las subidas.")
                    break
            except Exception as e:
                print(f"[{canal}] fallo la subida: {e}")

    print(f"\nClips listos: {listos} | Subidos a YouTube: {subidos}")


if __name__ == "__main__":
    main()
