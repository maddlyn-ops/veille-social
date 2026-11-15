"""Relevé quotidien des comptes TikTok et Instagram suivis.

- TikTok : yt-dlp (open source, sans compte ni clé).
- Instagram : Bright Data Scraper API (palier gratuit), si le secret
  BRIGHTDATA_API_KEY est défini. Sinon Instagram est ignoré proprement.

Le résultat est écrit dans data/social.json. Un compte en échec garde ses
derniers chiffres connus, marqués comme anciens.
"""
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "data" / "social.json"
DIGEST = ROOT / "data" / "digest.md"
BD_DATASET_POSTS = "gd_lk5ns7kz21pck8jpis"  # Instagram posts, discover by URL
BD_API = "https://api.brightdata.com/datasets/v3"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def short(text, limit=220):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def add_ratios(posts, metric):
    """Ajoute à chaque post son rapport à la médiane du compte sur `metric`."""
    values = [p[metric] for p in posts if p.get(metric)]
    median = statistics.median(values) if values else None
    for p in posts:
        p["ratio"] = round(p[metric] / median, 2) if median and p.get(metric) else None
    return median


# ---------------------------------------------------------------- TikTok

def fetch_tiktok(handle, limit):
    import yt_dlp  # installé par le workflow

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "playlistend": limit,
    }
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
        raise RuntimeError(str(last_error)[:300])

    posts = []
    for e in (info.get("entries") or [])[:limit]:
        if not e:
            continue
        ts = e.get("timestamp")
        posts.append({
            "url": e.get("webpage_url") or e.get("url") or f"https://www.tiktok.com/@{handle}/video/{e.get('id')}",
            "caption": short(e.get("description") or e.get("title")),
            "date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else None,
            "views": to_int(e.get("view_count")),
            "likes": to_int(e.get("like_count")),
            "comments": to_int(e.get("comment_count")),
            "shares": to_int(e.get("repost_count")),
            "type": "video",
        })
    if not posts:
        raise RuntimeError("aucune vidéo renvoyée")
    return posts


# ------------------------------------------------------------- Instagram

def bd_request(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def fetch_instagram_all(handles, limit, token):
    """Un seul job Bright Data pour tous les comptes. Renvoie {handle: posts}."""
    params = urllib.parse.urlencode({
        "dataset_id": BD_DATASET_POSTS,
        "type": "discover_new",
        "discover_by": "url",
        "include_errors": "true",
        "notify": "false",
    })
    body = {"input": [{"url": f"https://www.instagram.com/{h}/", "num_of_posts": limit} for h in handles]}
    status, text = bd_request("POST", f"{BD_API}/trigger?{params}", token, body)
    if status != 200:
        raise RuntimeError(f"Bright Data trigger {status}: {text[:300]}")
    snapshot_id = json.loads(text).get("snapshot_id")
    if not snapshot_id:
        raise RuntimeError(f"Bright Data: pas de snapshot_id ({text[:200]})")

    records = None
    for _ in range(60):  # jusqu'à 20 minutes
        time.sleep(20)
        status, text = bd_request("GET", f"{BD_API}/snapshot/{snapshot_id}?format=json", token)
        if status == 200:
            records = json.loads(text)
            break
        if status != 202:
            raise RuntimeError(f"Bright Data snapshot {status}: {text[:300]}")
    if records is None:
        raise RuntimeError("Bright Data: délai dépassé")
    return parse_instagram(records, handles, limit)


def handle_from_url(url):
    parts = [p for p in urllib.parse.urlparse(url or "").path.split("/") if p]
    return parts[0].lower() if parts else None


def parse_instagram(records, handles, limit):
    """Accepte les deux formes connues : posts à plat, ou profils avec `posts`."""
    wanted = {h.lower() for h in handles}
    by_handle = {h: [] for h in wanted}
    if isinstance(records, dict):
        records = [records]
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        owner = (rec.get("user_posted") or rec.get("account") or rec.get("profile_name") or "").lower()
        if owner not in wanted:
            owner = handle_from_url((rec.get("input") or {}).get("url") or rec.get("profile_url")) or owner
        items = rec.get("posts") if isinstance(rec.get("posts"), list) else [rec]
        for p in items:
            if not isinstance(p, dict) or not (p.get("url") or p.get("id")):
                continue
            if owner not in by_handle:
                continue
            date = p.get("datetime") or p.get("date_posted") or p.get("timestamp")
            by_handle[owner].append({
                "url": p.get("url"),
                "caption": short(p.get("caption") or p.get("description")),
                "date": str(date)[:10] if date else None,
                "views": to_int(p.get("video_view_count") or p.get("video_play_count") or p.get("views")),
                "likes": to_int(p.get("likes")),
                "comments": to_int(p.get("comments") if not isinstance(p.get("comments"), list) else p.get("num_comments")) or to_int(p.get("num_comments")),
                "shares": None,
                "type": str(p.get("content_type") or "post").lower(),
            })
    return {h: posts[:limit] for h, posts in by_handle.items()}


# ---------------------------------------------------------------- digest

def write_digest(result):
    """Résumé texte compact, facile à lire sans ouvrir le JSON complet."""
    lines = [f"# Relevé social du {result['generated_at']}", "",
             f"Comptes relevés : {result['ok']} sur {result['total']}", "",
             "## Comptes", ""]
    for a in result["accounts"]:
        if a["status"] == "ok":
            lines.append(f"- {a['platform']} @{a['handle']} : ok, médiane {a['median']:.0f} {a['metric']} sur {len(a['posts'])} posts")
        else:
            lines.append(f"- {a['platform']} @{a['handle']} : ERREUR, {a.get('error', '')[:160]}")
    lines += ["", "## Posts au moins 2 fois au-dessus de la médiane de leur compte (90 derniers jours)", ""]
    rows = []
    for a in result["accounts"]:
        if a["status"] != "ok":
            continue
        for p in a["posts"]:
            if (p.get("ratio") or 0) >= 2:
                rows.append((p.get("date") or "", a, p))
    cutoff = datetime.fromtimestamp(time.time() - 90 * 86400, timezone.utc).strftime("%Y-%m-%d")
    rows = [r for r in rows if r[0] >= cutoff]
    for date, a, p in sorted(rows, key=lambda r: r[0], reverse=True):
        lines.append(
            f"- {date} | {a['platform']} @{a['handle']} | ratio {p['ratio']} | vues {p.get('views')} | likes {p.get('likes')} "
            f"| commentaires {p.get('comments')} | partages {p.get('shares')} | {p.get('url')} | {short(p.get('caption'), 160)}"
        )
    if not rows:
        lines.append("- aucun")
    DIGEST.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ main

def build_account(platform, handle, posts, error, previous):
    metric = "views" if platform == "tiktok" else "likes"
    entry = {"platform": platform, "handle": handle, "metric": metric}
    if posts:
        entry.update({
            "status": "ok",
            "fetched_at": now_iso(),
            "median": add_ratios(posts, metric),
            "posts": posts,
        })
    else:
        old = previous.get((platform, handle), {})
        entry.update({
            "status": "erreur",
            "error": short(error or "aucun post renvoyé", 300),
            "fetched_at": old.get("fetched_at"),
            "median": old.get("median"),
            "posts": old.get("posts", []),
        })
    return entry


def main():
    cfg = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
    limit = int(cfg.get("posts_per_account", 10))
    previous = {}
    if OUT.exists():
        try:
            for a in json.loads(OUT.read_text(encoding="utf-8")).get("accounts", []):
                previous[(a["platform"], a["handle"])] = a
        except (ValueError, KeyError):
            pass

    accounts = []

    for handle in cfg.get("tiktok", []):
        posts, error = None, None
        try:
            posts = fetch_tiktok(handle, limit)
        except Exception as exc:
            error = str(exc)
        print(f"tiktok @{handle}: {'ok ' + str(len(posts)) if posts else 'ERREUR ' + (error or '')[:120]}")
        accounts.append(build_account("tiktok", handle, posts, error, previous))
        time.sleep(5)

    ig_handles = [h.lower() for h in cfg.get("instagram", [])]
    token = os.environ.get("BRIGHTDATA_API_KEY", "").strip()
    ig_posts, ig_error = {}, None
    if not ig_handles:
        pass
    elif not token:
        ig_error = "secret BRIGHTDATA_API_KEY absent : Instagram non relevé"
    else:
        try:
            ig_posts = fetch_instagram_all(ig_handles, limit, token)
        except Exception as exc:
            ig_error = str(exc)
    for handle in ig_handles:
        posts = ig_posts.get(handle) or None
        print(f"instagram @{handle}: {'ok ' + str(len(posts)) if posts else 'ERREUR ' + (ig_error or 'aucun post')[:120]}")
        accounts.append(build_account("instagram", handle, posts, ig_error, previous))

    ok = sum(1 for a in accounts if a["status"] == "ok")
    result = {"generated_at": now_iso(), "ok": ok, "total": len(accounts), "accounts": accounts}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_digest(result)
    print(f"Terminé : {ok}/{len(accounts)} comptes relevés.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
