"""Relevé des comptes TikTok et Instagram suivis, et des hashtags TikTok.

- Comptes TikTok : yt-dlp (open source, sans compte ni clé), chaque jour.
- Comptes Instagram : Bright Data Scraper API (palier gratuit), chaque jour,
  si le secret BRIGHTDATA_API_KEY est défini.
- Hashtags et mots-clés TikTok : Bright Data, une fois par semaine (le lundi)
  ou quand RUN_HASHTAGS=1, pour rester sous le quota gratuit.

Résultats : data/social.json (comptes), data/hashtags.json (hashtags) et
data/digest.md (résumé texte des deux). Un compte en échec garde ses derniers
chiffres connus.
"""
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "data" / "social.json"
OUT_TAGS = ROOT / "data" / "hashtags.json"
DIGEST = ROOT / "data" / "digest.md"
OUT_FEEDS = ROOT / "data" / "feeds.json"
BD_API = "https://api.brightdata.com/datasets/v3"
BD_INSTAGRAM_POSTS = "gd_lk5ns7kz21pck8jpis"  # Instagram posts, discover by URL
BD_TIKTOK_POSTS = "gd_lu702nij2f790tmv9h"     # TikTok posts, discover by keyword


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def short(text, limit=220):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def to_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        n = float(value)
        if n > 1e12:
            n /= 1000
        return datetime.fromtimestamp(n, timezone.utc).strftime("%Y-%m-%d")
    return str(value)[:10]


def add_ratios(posts, metric):
    """Ajoute à chaque post son rapport à la médiane du compte sur `metric`."""
    values = [p[metric] for p in posts if p.get(metric)]
    median = statistics.median(values) if values else None
    for p in posts:
        p["ratio"] = round(p[metric] / median, 2) if median and p.get(metric) else None
    return median


# ------------------------------------------------------- TikTok (yt-dlp)

def fetch_tiktok(handle, limit):
    import yt_dlp  # installé par le workflow

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
            if "private" in str(exc).lower():
                raise RuntimeError("compte privé ou identifiant erroné")
            time.sleep(20 * (attempt + 1))
    else:
        raise RuntimeError(str(last_error)[:300])

    posts = []
    for e in (info.get("entries") or [])[:limit]:
        if not e:
            continue
        posts.append({
            "url": e.get("webpage_url") or e.get("url") or f"https://www.tiktok.com/@{handle}/video/{e.get('id')}",
            "caption": short(e.get("description") or e.get("title")),
            "date": to_date(e.get("timestamp")),
            "views": to_int(e.get("view_count")),
            "likes": to_int(e.get("like_count")),
            "comments": to_int(e.get("comment_count")),
            "shares": to_int(e.get("repost_count")),
            "type": "video",
        })
    if not posts:
        raise RuntimeError("aucune vidéo renvoyée")
    return posts


# ----------------------------------------------------------- Bright Data

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


def bd_collect(dataset_id, discover_by, inputs, token, max_wait_min=25):
    """Lance un job Bright Data et attend le résultat (liste d'enregistrements)."""
    params = urllib.parse.urlencode({
        "dataset_id": dataset_id, "type": "discover_new", "discover_by": discover_by,
        "include_errors": "true", "notify": "false",
    })
    status, text = bd_request("POST", f"{BD_API}/trigger?{params}", token, {"input": inputs})
    if status != 200:
        raise RuntimeError(f"Bright Data trigger {status}: {text[:300]}")
    snapshot_id = json.loads(text).get("snapshot_id")
    if not snapshot_id:
        raise RuntimeError(f"Bright Data: pas de snapshot_id ({text[:200]})")
    for _ in range(max_wait_min * 3):
        time.sleep(20)
        status, text = bd_request("GET", f"{BD_API}/snapshot/{snapshot_id}?format=json", token)
        if status == 200:
            records = json.loads(text)
            return [records] if isinstance(records, dict) else (records or [])
        if status != 202:
            raise RuntimeError(f"Bright Data snapshot {status}: {text[:300]}")
    raise RuntimeError("Bright Data: délai dépassé")


def handle_from_url(url):
    parts = [p for p in urllib.parse.urlparse(url or "").path.split("/") if p]
    return parts[0].lstrip("@").lower() if parts else None


def parse_instagram(records, handles, limit):
    """Accepte les deux formes connues : posts à plat, ou profils avec `posts`."""
    wanted = {h.lower() for h in handles}
    by_handle = {h: [] for h in wanted}
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        owner = str(first(rec, "user_posted", "account", "profile_name") or "").lower()
        if owner not in wanted:
            src = rec.get("input") if isinstance(rec.get("input"), dict) else {}
            owner = handle_from_url(src.get("url") or rec.get("profile_url")) or owner
        items = rec.get("posts") if isinstance(rec.get("posts"), list) else [rec]
        for p in items:
            if not isinstance(p, dict) or not (p.get("url") or p.get("id")) or owner not in by_handle:
                continue
            comments = p.get("num_comments") if isinstance(p.get("comments"), list) else first(p, "comments", "num_comments")
            by_handle[owner].append({
                "url": p.get("url"),
                "caption": short(first(p, "caption", "description")),
                "date": to_date(first(p, "datetime", "date_posted", "timestamp")),
                "views": to_int(first(p, "video_view_count", "video_play_count", "views")),
                "likes": to_int(p.get("likes")),
                "comments": to_int(comments),
                "shares": None,
                "type": str(p.get("content_type") or "post").lower(),
            })
    return {h: posts[:limit] for h, posts in by_handle.items()}


def parse_tiktok_search(records):
    """Normalise les posts TikTok renvoyés par la recherche par mot-clé."""
    posts = []
    for p in records or []:
        if not isinstance(p, dict) or not p.get("url"):
            continue
        src = p.get("input") if isinstance(p.get("input"), dict) else {}
        disc = p.get("discovery_input") if isinstance(p.get("discovery_input"), dict) else {}
        caption = first(p, "description", "caption", "title") or ""
        tags = p.get("hashtags")
        if isinstance(tags, list):
            tags = [str(t.get("name") if isinstance(t, dict) else t).lstrip("#").lower() for t in tags]
        else:
            tags = [t.lower() for t in re.findall(r"#(\w+)", str(caption))]
        posts.append({
            "keyword": first(disc, "search_keyword") or first(src, "search_keyword") or p.get("search_keyword"),
            "url": p.get("url"),
            "author": first(p, "profile_username", "account_id", "author", "username"),
            "caption": short(caption),
            "date": to_date(first(p, "create_time", "date_posted", "create_date", "timestamp")),
            "views": to_int(first(p, "play_count", "video_play_count", "views", "view_count")),
            "likes": to_int(first(p, "digg_count", "likes", "like_count")),
            "comments": to_int(first(p, "comment_count", "num_comments", "comments")),
            "shares": to_int(first(p, "share_count", "shares")),
            "hashtags": [t for t in tags if t and t != "none"],
        })
    return posts


def build_hashtag_report(keywords, posts, per_keyword):
    by_kw = defaultdict(list)
    for p in posts:
        by_kw[str(p.get("keyword") or "").lower()].append(p)
    keyword_rows = []
    for kw in keywords:
        items = sorted(by_kw.get(kw.lower(), []), key=lambda p: p.get("views") or 0, reverse=True)[:per_keyword]
        views = [p["views"] for p in items if p.get("views")]
        keyword_rows.append({
            "keyword": kw,
            "found": len(items),
            "median_views": statistics.median(views) if views else None,
            "top": [{k: p[k] for k in ("url", "author", "caption", "date", "views", "likes", "comments", "shares")} for p in items[:5]],
        })
    # Hashtags réellement utilisés par ces vidéos, tous mots-clés confondus
    seen, usage = set(), defaultdict(list)
    for p in posts:
        if p["url"] in seen:
            continue
        seen.add(p["url"])
        for t in set(p.get("hashtags") or []):
            if p.get("views"):
                usage[t].append(p["views"])
    tags = [{"hashtag": t, "videos": len(v), "median_views": statistics.median(v)}
            for t, v in usage.items() if len(v) >= 3]
    tags.sort(key=lambda r: r["median_views"], reverse=True)
    return keyword_rows, tags[:40]


# ------------------------------------------------- flux d'articles (RSS)

def fetch_feed(url, limit=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; veille-bot)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        root = ET.fromstring(resp.read())
    items = []
    for it in root.iter("item"):
        date = None
        try:
            date = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
        author = it.findtext("{http://purl.org/dc/elements/1.1/}creator")
        items.append({
            "title": short(it.findtext("title"), 200),
            "url": (it.findtext("link") or "").split("?source=")[0],
            "date": date,
            "author": short(author, 80) if author else None,
            "tags": [short(c.text, 40) for c in it.findall("category") if c.text][:6],
        })
        if len(items) >= limit:
            break
    return items


def collect_feeds(feeds):
    out = []
    for f in feeds:
        entry = {"name": f["name"], "source": f.get("source", "web"), "url": f["url"]}
        try:
            entry.update({"status": "ok", "items": fetch_feed(f["url"])})
        except Exception as exc:
            entry.update({"status": "erreur", "error": short(str(exc), 200), "items": []})
        print(f"flux {f['name']}: {entry['status']} {len(entry['items'])}")
        out.append(entry)
    return out


# ---------------------------------------------------------------- digest

def write_digest(result, tags_result, feeds=None):
    lines = [f"# Relevé social du {result['generated_at']}", "",
             f"Comptes relevés : {result['ok']} sur {result['total']}", "", "## Comptes", ""]
    for a in result["accounts"]:
        if a["status"] == "ok":
            lines.append(f"- {a['platform']} @{a['handle']} : ok, médiane {a['median']:.0f} {a['metric']} sur {len(a['posts'])} posts")
        else:
            lines.append(f"- {a['platform']} @{a['handle']} : ERREUR, {str(a.get('error', ''))[:160]}")
    lines += ["", "## Posts au moins 2 fois au-dessus de la médiane de leur compte (90 derniers jours)", ""]
    cutoff = datetime.fromtimestamp(time.time() - 90 * 86400, timezone.utc).strftime("%Y-%m-%d")
    rows = []
    for a in result["accounts"]:
        if a["status"] != "ok":
            continue
        for p in a["posts"]:
            if (p.get("ratio") or 0) >= 2 and (p.get("date") or "") >= cutoff:
                rows.append((p.get("date") or "", a, p))
    for date, a, p in sorted(rows, key=lambda r: r[0], reverse=True):
        lines.append(
            f"- {date} | {a['platform']} @{a['handle']} | ratio {p['ratio']} | vues {p.get('views')} | likes {p.get('likes')} "
            f"| commentaires {p.get('comments')} | partages {p.get('shares')} | {p.get('url')} | {short(p.get('caption'), 160)}")
    if not rows:
        lines.append("- aucun")

    lines += ["", "## Hashtags et mots-clés TikTok (relevé hebdomadaire)", ""]
    if not tags_result:
        lines.append("- pas encore de relevé")
    elif tags_result.get("status") != "ok":
        lines.append(f"- ERREUR au relevé du {tags_result.get('generated_at')} : {str(tags_result.get('error'))[:200]}")
    else:
        lines.append(f"Relevé du {tags_result['generated_at']}.")
        lines.append("À lire avec prudence : ce sont les vidéos les mieux classées par la recherche TikTok, toutes dates confondues "
                     "(regarder la date de chaque vidéo). Les médianes montrent où existent de grosses vidéos, pas ce qu'obtient une vidéo moyenne. "
                     "Les mots-clés très larges (ux, ui, workinggirl, product design) ramènent du contenu hors sujet.")
        lines += ["", "### Par mot-clé : médiane de vues des vidéos trouvées, puis les 3 meilleures", ""]
        for k in sorted(tags_result["keywords"], key=lambda r: r["median_views"] or 0, reverse=True):
            lines.append(f"- {k['keyword']} : {k['found']} vidéos, médiane {k['median_views']} vues")
            for p in k["top"][:3]:
                lines.append(f"    - {p.get('date')} | @{p.get('author')} | vues {p.get('views')} | likes {p.get('likes')} | partages {p.get('shares')} | {p.get('url')} | {short(p.get('caption'), 140)}")
        lines += ["", "### Hashtags les plus présents sur ces vidéos, classés par médiane de vues", ""]
        for t in tags_result["hashtags"][:30]:
            lines.append(f"- #{t['hashtag']} : {t['videos']} vidéos, médiane {t['median_views']:.0f} vues")
    lines += ["", "## Articles récents des sources suivies (30 derniers jours)", ""]
    cutoff30 = datetime.fromtimestamp(time.time() - 30 * 86400, timezone.utc).strftime("%Y-%m-%d")
    for f in feeds or []:
        if f["status"] != "ok":
            lines.append(f"- {f['name']} : ERREUR, {f.get('error')}")
            continue
        recent = [i for i in f["items"] if (i.get("date") or "") >= cutoff30]
        lines.append(f"- {f['name']} (source {f['source']}) : {len(recent)} articles récents")
        for i in recent:
            by = f" | par {i['author']}" if i.get("author") else ""
            lines.append(f"    - {i['date']} | {i['title']}{by} | {i['url']} | {', '.join(i['tags'])}")
    if not feeds:
        lines.append("- aucun flux configuré")
    DIGEST.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ main

def build_account(platform, handle, posts, error, previous):
    metric = "views" if platform == "tiktok" else "likes"
    entry = {"platform": platform, "handle": handle, "metric": metric}
    if posts:
        entry.update({"status": "ok", "fetched_at": now_iso(),
                      "median": add_ratios(posts, metric), "posts": posts})
    else:
        old = previous.get((platform, handle), {})
        entry.update({"status": "erreur", "error": short(error or "aucun post renvoyé", 300),
                      "fetched_at": old.get("fetched_at"), "median": old.get("median"),
                      "posts": old.get("posts", [])})
    return entry


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main():
    cfg = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
    limit = int(cfg.get("posts_per_account", 10))
    token = os.environ.get("BRIGHTDATA_API_KEY", "").strip()
    previous = {}
    for a in (load_json(OUT) or {}).get("accounts", []):
        previous[(a.get("platform"), a.get("handle"))] = a

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
    ig_posts, ig_error, ig_keys = {}, None, None
    # Instagram : un jour sur deux (quota gratuit), sur demande, ou si un compte n'a jamais été relevé
    ig_due = (os.environ.get("RUN_INSTAGRAM") == "1"
              or datetime.now(timezone.utc).timetuple().tm_yday % 2 == 0
              or any(("instagram", h) not in previous for h in ig_handles))
    if ig_handles and not ig_due:
        for handle in ig_handles:
            accounts.append(previous[("instagram", handle)])
        print("instagram: pas de relevé aujourd'hui (un jour sur deux), chiffres précédents conservés")
        ig_handles = []
    if ig_handles and not token:
        ig_error = "secret BRIGHTDATA_API_KEY absent : Instagram non relevé"
    elif ig_handles:
        try:
            inputs = [{"url": f"https://www.instagram.com/{h}/", "num_of_posts": limit} for h in ig_handles]
            records = bd_collect(BD_INSTAGRAM_POSTS, "url", inputs, token)
            ig_keys = sorted(records[0].keys())[:60] if records and isinstance(records[0], dict) else None
            ig_posts = parse_instagram(records, ig_handles, limit)
        except Exception as exc:
            ig_error = str(exc)
    for handle in ig_handles:
        posts = ig_posts.get(handle) or None
        print(f"instagram @{handle}: {'ok ' + str(len(posts)) if posts else 'ERREUR ' + (ig_error or 'aucun post')[:120]}")
        accounts.append(build_account("instagram", handle, posts, ig_error, previous))

    ok = sum(1 for a in accounts if a["status"] == "ok")
    result = {"generated_at": now_iso(), "ok": ok, "total": len(accounts),
              "debug_instagram_fields": ig_keys, "accounts": accounts}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Hashtags : le lundi, ou sur demande, ou s'il n'y a encore aucun relevé réussi
    keywords = cfg.get("hashtags", [])
    tags_result = load_json(OUT_TAGS)
    due = (os.environ.get("RUN_HASHTAGS") == "1" or datetime.now(timezone.utc).weekday() == 0
           or not tags_result or tags_result.get("status") != "ok")
    if keywords and due:
        per_kw = int(cfg.get("posts_per_hashtag", 8))
        if not token:
            new = {"generated_at": now_iso(), "status": "erreur", "error": "secret BRIGHTDATA_API_KEY absent"}
        else:
            try:
                inputs = [{"search_keyword": k, "num_of_posts": per_kw} for k in keywords]
                records = bd_collect(BD_TIKTOK_POSTS, "keyword", inputs, token)
                posts = parse_tiktok_search(records)
                got = {str(p.get("keyword") or "").lower() for p in posts}
                missing = [k for k in keywords if k.lower() not in got]
                if missing:  # deuxième essai pour les mots-clés revenus vides
                    try:
                        retry = bd_collect(BD_TIKTOK_POSTS, "keyword",
                                           [{"search_keyword": k, "num_of_posts": per_kw} for k in missing], token)
                        posts += parse_tiktok_search(retry)
                    except Exception as exc:
                        print(f"hashtags: deuxième essai en échec ({str(exc)[:100]})")
                rows, tags = build_hashtag_report(keywords, posts, per_kw)
                new = {"generated_at": now_iso(), "status": "ok",
                       "debug_fields": sorted(records[0].keys())[:80] if records and isinstance(records[0], dict) else None,
                       "keywords": rows, "hashtags": tags}
            except Exception as exc:
                new = {"generated_at": now_iso(), "status": "erreur", "error": short(str(exc), 300)}
        print(f"hashtags: {new['status']} {new.get('error', '')[:120]}")
        # Un échec ne remplace pas un ancien relevé réussi
        if new["status"] == "ok" or not tags_result or tags_result.get("status") != "ok":
            tags_result = new
            OUT_TAGS.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    feeds = collect_feeds(cfg.get("feeds", []))
    OUT_FEEDS.write_text(json.dumps({"generated_at": now_iso(), "feeds": feeds}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_digest(result, tags_result, feeds)
    print(f"Terminé : {ok}/{len(accounts)} comptes relevés.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
