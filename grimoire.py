"""Pilote du grimoire : collecte complète de quelques comptes TikTok.

Pour chaque compte : liste des posts et stats (yt-dlp), puis pour chaque post
- vidéo : téléchargement, repérage des moments visuels (inserts, captures,
  changements de plan), planches d'images clés, transcription (faster-whisper)
- carrousel photo : images des slides (gallery-dl)

Rien n'est écrit dans le dépôt : le dossier out/ part dans un artefact du
workflow, conservé 1 jour. Les vidéos et l'audio sont supprimés après
traitement : il ne reste que le texte, les stats et des images réduites.

Variables d'environnement :
- GRIMOIRE_ACCOUNTS : "compte:role,compte:role" (role = prof ou modele)
- GRIMOIRE_LIMIT : nombre de posts par compte (20 par défaut)
- WHISPER_MODEL : modèle de transcription (small par défaut)
- GRIMOIRE_BUDGET_MIN : durée maximale de traitement, en minutes (60 par défaut)
- GRIMOIRE_SKIP_WHISPER=1 : ne pas transcrire (réglage des images uniquement)
- GRIMOIRE_DIAG=1 : garder la bande de vignettes et les scores de chaque vidéo
"""
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "out"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic"}
FRAME_WIDTH = 360      # largeur d'une image clé dans une planche
SHEET_COLS, SHEET_ROWS = 4, 2
MAX_FRAMES = 40        # images clés par vidéo, au plus
MAX_SLIDES = 20
SLIDE_WIDTH = 720
TW, TH = 72, 128       # vignettes servant au repérage des moments visuels
MAX_THUMBS = 600
PIXEL_DELTA = 45       # écart de gris à partir duquel un pixel compte comme changé
EVENT_DELTA = 0.08     # part de l'image qui doit sortir du fond habituel
EVENT_CHANGE = 0.10    # changement minimal entre deux images d'un même moment
CUT_CHANGE = 0.30      # changement de plan, pour les vidéos sans fond stable
STATIC_LIMIT = 0.30    # en dessous : fond stable (face caméra, plan fixe)
ORIGINAL_SOUND = ("original sound", "son original", "sonido original", "originalton",
                  "suono originale", "som original", "오리지널", "оригинальный")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def to_date(value):
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(value)[:10]


def clip(text, limit=300):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ------------------------------------------------------------ liste des posts

def list_posts(handle, limit):
    """Liste les derniers posts d'un compte avec leurs stats publiques."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": True, "playlistend": limit}
    last_error = None
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.tiktok.com/@{handle}", download=False)
            break
        except Exception as exc:  # TikTok refuse parfois selon l'adresse IP
            last_error = exc
            time.sleep(20 * (attempt + 1))
    else:
        raise RuntimeError(clip(last_error))

    posts = []
    for e in (info.get("entries") or [])[:limit]:
        if not e or not e.get("id"):
            continue
        posts.append({
            "id": str(e["id"]),
            "url": e.get("webpage_url") or e.get("url") or f"https://www.tiktok.com/@{handle}/video/{e['id']}",
            "date": to_date(e.get("timestamp")),
            "caption": " ".join(str(e.get("description") or e.get("title") or "").split()),
            "duration": to_int(e.get("duration")),
            "views": to_int(e.get("view_count")),
            "likes": to_int(e.get("like_count")),
            "comments": to_int(e.get("comment_count")),
            "shares": to_int(e.get("repost_count")),
            "saves": to_int(e.get("save_count")),
        })
    if not posts:
        raise RuntimeError("aucun post renvoyé")
    views = [p["views"] for p in posts if p.get("views")]
    median = statistics.median(views) if views else None
    for p in posts:
        p["ratio"] = round(p["views"] / median, 2) if median and p.get("views") else None
    return posts, median


# ------------------------------------------------------------------- médias

def probe(path):
    """Renvoie (a_une_piste_video, durée en secondes)."""
    video = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=codec_name", "-of", "csv=p=0", str(path)]).stdout.strip()
    dur = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)]).stdout.strip()
    try:
        duration = float(dur)
    except ValueError:
        duration = 0.0
    return bool(video) and video not in ("mjpeg", "png"), duration


def sound_of(info):
    """Le son utilisé : son original du créateur, ou morceau du catalogue."""
    track = info.get("track")
    artists = info.get("artists") or ([info.get("artist")] if info.get("artist") else [])
    original = None
    if track:
        original = any(k in str(track).lower() for k in ORIGINAL_SOUND)
    return {"track": clip(track, 120) if track else None,
            "artist": clip(", ".join(str(a) for a in artists if a), 120) or None,
            "original": original}


def download_media(url, workdir):
    """Télécharge la vidéo (ou, pour un carrousel, seulement sa bande son)."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "noprogress": True,
            "format": "b[height<=720]/b/ba", "outtmpl": str(workdir / "media.%(ext)s"),
            "retries": 2, "socket_timeout": 30}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}
    files = sorted(workdir.glob("media.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("aucun fichier téléchargé")
    return files[0], info


def download_slides(url, workdir):
    """Récupère les images d'un carrousel photo avec gallery-dl."""
    raw = workdir / "raw"
    raw.mkdir(exist_ok=True)
    photo_url = url.replace("/video/", "/photo/")
    res = run(["gallery-dl", "-q", "-D", str(raw), "-o", "videos=false", "-o", "audio=false",
               photo_url], timeout=240)
    images = sorted(p for p in raw.rglob("*") if p.suffix.lower() in IMAGE_EXT)
    if not images:
        raise RuntimeError(clip(res.stderr or res.stdout or "aucune image renvoyée"))
    return images


def save_slides(images, outdir):
    from PIL import Image

    names = []
    for i, src in enumerate(images[:MAX_SLIDES], 1):
        try:
            im = Image.open(src).convert("RGB")
        except Exception:
            continue
        if im.width > SLIDE_WIDTH:
            im = im.resize((SLIDE_WIDTH, round(im.height * SLIDE_WIDTH / im.width)))
        name = f"slide_{i:02d}.jpg"
        im.save(outdir / name, quality=82)
        names.append(name)
    return names


def stamp(seconds):
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


# ------------------------------------------------ repérage des moments visuels

def scan_video(media, duration, workdir):
    """Vignettes à intervalle fixe, avec deux mesures par vignette :
    `bg` = part de l'image qui sort du fond habituel de la vidéo (insert, capture, autre plan),
    `change` = part de l'image qui a changé depuis la vignette précédente."""
    import numpy as np
    from PIL import Image

    fps = 2 if duration < 20 else 1
    tdir = workdir / "thumbs"
    tdir.mkdir(exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(media), "-vf", f"fps={fps},scale={TW}:{TH}",
         "-q:v", "6", str(tdir / "t_%04d.jpg")], timeout=600)
    files = sorted(tdir.glob("t_*.jpg"))[:MAX_THUMBS]
    if len(files) < 3:
        return None
    arr = np.stack([np.asarray(Image.open(f).convert("L").resize((TW, TH)), dtype=np.int16) for f in files])
    base = np.median(arr, axis=0)
    bg = (np.abs(arr - base) > PIXEL_DELTA).mean(axis=(1, 2))
    change = np.concatenate([[0.0], (np.abs(arr[1:] - arr[:-1]) > PIXEL_DELTA).mean(axis=(1, 2))])
    return {"fps": fps, "times": [round(i / fps, 1) for i in range(len(files))],
            "bg": [round(float(v), 3) for v in bg], "change": [round(float(v), 3) for v in change],
            "arr": arr, "files": files}


def pick_times(times, bg, arr, duration):
    """Choisit les images à garder : les moments visuels, plus des repères réguliers.
    Renvoie une liste triée de (temps, est_un_moment_visuel)."""
    import numpy as np

    n = len(times)
    base = statistics.median(bg)
    static = base < STATIC_LIMIT

    def diff(i, j):
        return float((np.abs(arr[i] - arr[j]) > PIXEL_DELTA).mean())

    events, last, i = [], None, 0
    while i < n:
        if static:
            if bg[i] >= base + EVENT_DELTA:
                if last is None:
                    # début d'un insert : on prend l'image suivante si elle tient, pour éviter la transition
                    j = i + 1 if i + 1 < n and bg[i + 1] >= base + EVENT_DELTA else i
                    events.append(j)
                    last, i = j, j
                elif diff(i, last) >= EVENT_CHANGE:
                    events.append(i)
                    last = i
            else:
                last = None
        else:
            if last is None or (diff(i, last) >= CUT_CHANGE and times[i] - times[last] >= 1):
                events.append(i)
                last = i
        i += 1

    step = max(3.0, duration / 10) if duration else 3.0
    regular, t = [], 0.0
    while t < max(duration, times[-1]) - 0.5:
        idx = min(range(n), key=lambda k: abs(times[k] - t))
        if all(abs(times[idx] - times[e]) > 1.5 for e in events) and idx not in regular:
            regular.append(idx)
        t += step

    room = max(MAX_FRAMES - len(regular), 8)
    if len(events) > room:
        k = math.ceil(len(events) / room)
        events = events[::k]
    picks = {idx: False for idx in regular}
    picks.update({idx: True for idx in events})
    return sorted((times[idx], flag) for idx, flag in picks.items())


def save_strip(scan, outdir):
    """Bande de toutes les vignettes, pour régler les seuils sans la vidéo."""
    from PIL import Image

    files = scan["files"]
    cols = 24
    rows = (len(files) + cols - 1) // cols
    strip = Image.new("L", (TW * cols, TH * rows), 255)
    for i, f in enumerate(files):
        strip.paste(Image.open(f).convert("L").resize((TW, TH)), ((i % cols) * TW, (i // cols) * TH))
    strip.save(outdir / "bande.jpg", quality=70)
    (outdir / "scan.json").write_text(json.dumps(
        {k: scan[k] for k in ("fps", "times", "bg", "change")}), encoding="utf-8")


def make_sheets(media, picks, workdir, outdir):
    """Planches d'images clés. Étiquette orange = moment visuel, noire = repère régulier."""
    from PIL import Image, ImageDraw, ImageFont

    frames = []
    for i, (ts, flag) in enumerate(picks):
        dst = workdir / f"f_{i:03d}.jpg"
        run(["ffmpeg", "-y", "-v", "error", "-ss", str(max(ts, 0.3)), "-i", str(media), "-frames:v", "1",
             "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", str(dst)], timeout=60)
        if dst.exists() and dst.stat().st_size:
            frames.append((ts, flag, dst))
    if not frames:
        return []

    try:
        font = ImageFont.load_default(size=20)
    except TypeError:
        font = ImageFont.load_default()
    names = []
    # L'accroche seule, en plus grand, pour lire le texte à l'écran
    hook = workdir / "hook.jpg"
    run(["ffmpeg", "-y", "-v", "error", "-ss", "0.3", "-i", str(media), "-frames:v", "1",
         "-vf", "scale=540:-2", "-q:v", "3", str(hook)], timeout=60)
    if hook.exists() and hook.stat().st_size:
        shutil.copy(hook, outdir / "hook.jpg")
        names.append("hook.jpg")

    per_sheet = SHEET_COLS * SHEET_ROWS
    fw, fh = Image.open(frames[0][2]).size
    for s in range(0, len(frames), per_sheet):
        chunk = frames[s:s + per_sheet]
        rows = (len(chunk) + SHEET_COLS - 1) // SHEET_COLS
        sheet = Image.new("RGB", (fw * SHEET_COLS, fh * rows), "white")
        draw = ImageDraw.Draw(sheet)
        for i, (ts, flag, path) in enumerate(chunk):
            x, y = (i % SHEET_COLS) * fw, (i // SHEET_COLS) * fh
            sheet.paste(Image.open(path).convert("RGB").resize((fw, fh)), (x, y))
            draw.rectangle([x, y, x + 62, y + 28], fill=(230, 110, 0) if flag else "black")
            draw.text((x + 6, y + 3), stamp(ts), fill="white", font=font)
        name = f"planche_{s // per_sheet + 1:02d}.jpg"
        sheet.save(outdir / name, quality=80)
        names.append(name)
    return names


def regular_picks(duration):
    step = max(2.0, duration / 15) if duration else 2.0
    picks, t = [(0.3, False)], step
    while t < max(duration - 0.2, 0) and len(picks) < 16:
        picks.append((round(t, 1), False))
        t += step
    return picks


# ------------------------------------------------------------ transcription

_MODEL = None


def transcribe(media, workdir):
    global _MODEL
    wav = workdir / "audio.wav"
    empty = {"language": None, "text": "", "segments": [], "speech_seconds": 0}
    res = run(["ffmpeg", "-y", "-v", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", str(wav)])
    if not wav.exists() or not wav.stat().st_size:
        return {**empty, "note": clip(res.stderr or "pas de piste audio", 120)}
    # Le son est lu ici (wave + numpy) plutôt que par le décodeur de faster-whisper,
    # qui casse avec les versions récentes de PyAV.
    import wave
    import numpy as np
    with wave.open(str(wav), "rb") as fh:
        audio = np.frombuffer(fh.readframes(fh.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if audio.size < 16000:  # moins d'une seconde de son
        return {**empty, "note": "son trop court"}
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu",
                              compute_type="int8", cpu_threads=os.cpu_count() or 2)
    segments, info = _MODEL.transcribe(audio, vad_filter=True, beam_size=5)
    rows = [{"start": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip()} for s in segments]
    return {
        "language": info.language,
        "language_probability": round(float(info.language_probability or 0), 2),
        "text": " ".join(r["text"] for r in rows),
        "segments": rows,
        "speech_seconds": round(sum(r["end"] - r["start"] for r in rows), 1),
    }


def voice_label(transcript, sound):
    """parole / aucune / a_verifier_musique (texte transcrit sur un morceau du catalogue :
    ce sont peut-être les paroles de la chanson, à trancher à la lecture)."""
    if (transcript or {}).get("speech_seconds", 0) < 2:
        return "aucune"
    if (sound or {}).get("original") is False:
        return "a_verifier_musique"
    return "parole"


# --------------------------------------------------------------- un post

def process_post(post, outdir):
    """Complète `post` : type (video ou photo), fichiers produits, transcription."""
    work = outdir / "_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        media, info, media_error = None, {}, None
        try:
            media, info = download_media(post["url"], work)
        except Exception as exc:
            media_error = clip(exc)
        has_video, duration = probe(media) if media else (False, 0.0)
        post["sound"] = sound_of(info)

        if has_video:
            post["kind"] = "video"
            post["duration"] = post.get("duration") or round(duration)
            picks, scan = None, None
            try:
                scan = scan_video(media, duration, work)
                if scan:
                    picks = pick_times(scan["times"], scan["bg"], scan["arr"], duration)
                    post["visuals"] = {"fond_stable": statistics.median(scan["bg"]) < STATIC_LIMIT,
                                       "moments": [t for t, flag in picks if flag]}
                    if os.environ.get("GRIMOIRE_DIAG") == "1":
                        save_strip(scan, outdir)
            except Exception as exc:
                post["visuals"] = {"note": "ERREUR " + clip(exc, 200)}
            post["files"] = make_sheets(media, picks or regular_picks(duration), work, outdir)
            if os.environ.get("GRIMOIRE_SKIP_WHISPER") == "1":
                post["transcript"] = {"language": None, "text": "", "segments": [],
                                      "speech_seconds": 0, "note": "transcription désactivée"}
            else:
                try:
                    post["transcript"] = transcribe(media, work)
                except Exception as exc:  # les images restent utiles même sans texte
                    post["transcript"] = {"language": None, "text": "", "segments": [],
                                          "speech_seconds": 0, "note": "ERREUR " + clip(exc, 200)}
            post["voice"] = voice_label(post["transcript"], post["sound"])
        else:
            # Pas de piste vidéo : carrousel photo, ou téléchargement refusé
            try:
                post["files"] = save_slides(download_slides(post["url"], work), outdir)
                post["kind"] = "photo"
                post["slides"] = len(post["files"])
            except Exception as exc:
                post["kind"] = "inconnu"
                raise RuntimeError(f"vidéo : {media_error or 'pas de piste vidéo'} | photos : {clip(exc, 200)}")
        post["status"] = "ok"
    except Exception as exc:
        post["status"] = "erreur"
        post["error"] = clip(exc, 400)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return post


# ------------------------------------------------------------------ rapport

def write_report(accounts, started):
    lines = [f"# Pilote grimoire du {now_iso()}", "",
             f"Durée : {round((time.time() - started) / 60, 1)} min", ""]
    for a in accounts:
        posts = a.get("posts") or []
        ok = [p for p in posts if p.get("status") == "ok"]
        videos = [p for p in ok if p.get("kind") == "video"]
        photos = [p for p in ok if p.get("kind") == "photo"]
        lines += [f"## @{a['handle']} ({a['role']})", ""]
        if a.get("error"):
            lines += [f"ERREUR à la liste des posts : {a['error']}", ""]
            continue
        lines += [f"- posts listés : {len(posts)}, médiane {a.get('median_views')} vues",
                  f"- traités : {len(ok)} (vidéos {len(videos)}, carrousels {len(photos)}), en erreur : {len([p for p in posts if p.get('status') == 'erreur'])}, non traités : {len([p for p in posts if not p.get('status')])}",
                  f"- voix : parole {len([p for p in videos if p.get('voice') == 'parole'])}, à vérifier (musique) {len([p for p in videos if p.get('voice') == 'a_verifier_musique'])}, aucune {len([p for p in videos if p.get('voice') == 'aucune'])}",
                  f"- enregistrements disponibles : {'oui' if any(p.get('saves') is not None for p in posts) else 'non'}",
                  "", "| date | type | vues | ratio | statut | voix | parole (s) | moments visuels | images | son | post |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in posts:
            tr = p.get("transcript") or {}
            snd = p.get("sound") or {}
            lines.append(f"| {p.get('date')} | {p.get('kind', '')} | {p.get('views')} | {p.get('ratio')} | "
                         f"{p.get('status', 'non traité')} | {p.get('voice', '')} | {tr.get('speech_seconds', '')} | "
                         f"{len((p.get('visuals') or {}).get('moments') or [])} | {len(p.get('files') or [])} | "
                         f"{clip(snd.get('track'), 30)} | "
                         f"{p['id']} {clip(p.get('error') or (p.get('visuals') or {}).get('note') or tr.get('note') or p.get('caption'), 60)} |")
        lines.append("")
    text = "\n".join(lines) + "\n"
    (OUT / "rapport.md").write_text(text, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    print(text)


# --------------------------------------------------------------------- main

def main():
    spec = os.environ.get("GRIMOIRE_ACCOUNTS", "").strip()
    if not spec:
        print("GRIMOIRE_ACCOUNTS est vide")
        return 1
    limit = int(os.environ.get("GRIMOIRE_LIMIT", "20") or 20)
    budget = float(os.environ.get("GRIMOIRE_BUDGET_MIN", "60") or 60) * 60
    started = time.time()
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)

    accounts = []
    for item in spec.split(","):
        handle, _, role = item.strip().lstrip("@").partition(":")
        if not handle:
            continue
        entry = {"handle": handle, "role": role or "modele", "fetched_at": now_iso()}
        try:
            entry["posts"], entry["median_views"] = list_posts(handle, limit)
            print(f"@{handle}: {len(entry['posts'])} posts listés", flush=True)
        except Exception as exc:
            entry["error"] = clip(exc)
            print(f"@{handle}: ERREUR liste {entry['error']}", flush=True)
        accounts.append(entry)
        time.sleep(5)

    # Traitement post par post, en alternant les comptes pour répartir le budget
    queue = []
    longest = max((len(a.get("posts") or []) for a in accounts), default=0)
    for i in range(longest):
        for a in accounts:
            if i < len(a.get("posts") or []):
                queue.append((a, a["posts"][i]))
    for a, post in queue:
        if time.time() - started > budget:
            print("budget de temps atteint, arrêt du traitement", flush=True)
            break
        t0 = time.time()
        process_post(post, OUT / a["handle"] / post["id"])
        tr = post.get("transcript") or {}
        print(f"@{a['handle']} {post['id']}: {post.get('kind')} {post['status']} "
              f"{len(post.get('files') or [])} images, {len((post.get('visuals') or {}).get('moments') or [])} moments, "
              f"voix {post.get('voice')}, parole {tr.get('speech_seconds', 0)} s, "
              f"{round(time.time() - t0)} s {post.get('error', '')[:160]}", flush=True)
        time.sleep(3)

    for a in accounts:
        folder = OUT / a["handle"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "posts.json").write_text(json.dumps(a, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_report(accounts, started)
    done = sum(1 for a in accounts for p in a.get("posts") or [] if p.get("status") == "ok")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
