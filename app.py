"""
Video Scraper Web App (v2)
--------------------------
Paste a page URL -> get cards for every video found, each with download links.

Detection pipeline (first stage that finds something wins, results are merged):
  1. yt-dlp                      - platforms, playlists, HLS/DASH
  2. Static HTML scan            - <video>/<source>, og:video, media links, raw URLs,
                                   YouTube + Vimeo embeds in ANY attribute or script
  3. Headless-browser render     - OPTIONAL (Playwright). Runs the page's JavaScript so
                                   players injected after load (course sites etc.) show up
  4. Direct paste                - you can paste a .m3u8/.mpd/.mp4 URL or a
                                   player.vimeo.com/... embed URL straight in

Embedded players are often locked to the site that hosts them, so every download sends the
page URL as Referer (and its origin as Origin). Use the optional "Referer" field when you
paste an embed/stream URL directly.

Run:
  pip install -r requirements.txt
  python app.py            -> http://127.0.0.1:5000

Optional extras:
  ffmpeg                                   MP3 extraction + HD merging
  pip install playwright && playwright install chromium     JS-rendered pages

DRM-protected streams cannot be downloaded. Only download content you have the right to.
"""

import html as htmllib
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urljoin, urlparse

import requests
import yt_dlp
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template_string, request, send_file, stream_with_context

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

app = Flask(__name__)

# --------------------------------------------------------------------------- #
# Tiny in-memory rate limiter (per client IP) + health check
# --------------------------------------------------------------------------- #
if os.environ.get("TRUST_PROXY") == "1":  # behind nginx/Caddy: use the real client IP
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def hit_count(key: str, window: int) -> int:
    """Number of recorded hits for `key` in the last `window` seconds."""
    now = time.time()
    with _hits_lock:
        q = _hits[key]
        while q and q[0] < now - window:
            q.popleft()
        return len(q)


def record_hit(key: str):
    with _hits_lock:
        _hits[key].append(time.time())


def client_ip() -> str:
    return request.remote_addr or "unknown"


@app.get("/healthz")
def healthz():
    return "ok"


HAS_FFMPEG = shutil.which("ffmpeg") is not None
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE") == "1"
MAX_LINKED_PAGES = int(os.environ.get("MAX_LINKED_PAGES", "20"))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MEDIA_EXT = r"(?:mp4|webm|m3u8|mpd|mov|mkv|m4v|mp3|m4a|ogg|wav)"
DIRECT_RE = re.compile(r"\.(?:mp4|webm|mov|mkv|m4v|mp3|m4a|ogg|wav)(?:\?|$)", re.I)
STREAM_PATH_RE = re.compile(r"\." + MEDIA_EXT + r"$", re.I)
URL_RE = re.compile(r"https?:(?:\\?/){2}[^\s\"'<>\\]+?\." + MEDIA_EXT + r"(?:\?[^\s\"'<>\\]*)?", re.I)

YT_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:embed|shorts)/|youtu\.be/|youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})"
)
VIMEO_PLAYER_RE = re.compile(r"(?:https?:)?//player\.vimeo\.com/video/\d+(?:\?[^\s\"'<>\\]*)?", re.I)
VIMEO_PAGE_RE = re.compile(r"vimeo\.com/(\d{6,})", re.I)
URL_ATTRS = ("href", "src", "data-src", "data-lazy-src", "data-video", "data-url", "data-embed", "data-vimeo-id")

# Links never worth following when scanning a course/index page
SKIP_LINK_RE = re.compile(
    r"(?:^|/)(?:log-?in|log-?out|sign-?up|register|cart|checkout|privacy|terms|cookies?|account|cdn-cgi)(?:/|$)"
    r"|\.(?:css|js|png|jpe?g|gif|svg|webp|ico|pdf|zip|xml|json)$",
    re.I,
)
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")  # "00:18:30" duration badges in link text


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def is_safe_url(url: str) -> bool:
    """Basic SSRF guard: http(s) only, and no private/loopback addresses."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    if ALLOW_PRIVATE:
        return True
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def is_stream_url(url: str) -> bool:
    return bool(STREAM_PATH_RE.search(urlparse(url).path))


def fmt_duration(sec):
    if not sec:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def filename_from_url(url: str) -> str:
    return os.path.basename(unquote(urlparse(url).path)) or "video"


def request_headers(ref: str) -> dict:
    headers = {"User-Agent": UA}
    if ref:
        p = urlparse(ref)
        headers["Referer"] = ref
        if p.scheme and p.netloc:
            headers["Origin"] = f"{p.scheme}://{p.netloc}"
    return headers


def platform_url(raw: str):
    """Recognise YouTube/Vimeo links (embed, short, player) -> (canonical_url, thumbnail)."""
    raw = htmllib.unescape((raw or "").replace("\\/", "/"))
    m = YT_RE.search(raw)
    if m:
        vid = m.group(1)
        return f"https://www.youtube.com/watch?v={vid}", f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    m = VIMEO_PLAYER_RE.search(raw)  # keep the ?h=... hash, private embeds need it
    if m:
        u = m.group(0)
        return ("https:" + u if u.startswith("//") else u), None
    m = VIMEO_PAGE_RE.search(raw)
    if m:
        return f"https://vimeo.com/{m.group(1)}", None
    return None, None


def make_item(url, title=None, thumb=None, source="html", duration=""):
    return {
        "title": title or filename_from_url(url),
        "url": url,
        "direct": url if DIRECT_RE.search(urlparse(url).path + "?") else None,
        "thumbnail": thumb,
        "duration": duration,
        "source": source,
    }


def merge(results: list, new_items: list):
    known = {i["url"] for i in results}
    for item in new_items:
        if item["url"] not in known:
            results.append(item)
            known.add(item["url"])


def friendly_error(exc) -> str:
    msg = re.sub(r"\x1b?\[[0-9;]*m", "", str(exc)).strip()
    low = msg.lower()
    if "drm" in low:
        return "This video is DRM-protected and can't be downloaded."
    if any(k in low for k in ("403", "forbidden", "private", "unauthorized", "login", "referer")):
        msg += (
            "\n\nHint: the host may only allow playback from the site that embeds it. "
            "Scan again with the 'Referer' field set to that page's URL."
        )
    return msg


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def extract_with_ytdlp(page_url: str, ref: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": 50,
        "socket_timeout": 15,
        "http_headers": request_headers(ref),
    }
    items = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
    except Exception:
        return items
    if not info:
        return items

    entries = info.get("entries")
    if entries is None:
        entries = [info]

    for e in entries:
        if not e:
            continue
        url = e.get("webpage_url") or e.get("url") or (page_url if e is info else None)
        if not url or not url.startswith("http"):
            continue
        thumb = e.get("thumbnail")
        if not thumb and e.get("thumbnails"):
            thumb = e["thumbnails"][-1].get("url")
        items.append(make_item(
            url, e.get("title"), thumb, "yt-dlp", fmt_duration(e.get("duration"))
        ))
    return items


def fetch_html(page_url: str):
    try:
        r = requests.get(page_url, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        return r.text
    except requests.RequestException:
        return None


def parse_html(html: str, page_url: str):
    items, seen = [], set()

    def add(raw, title=None, thumb=None, source="html"):
        if not raw:
            return
        url = urljoin(page_url, htmllib.unescape(raw.replace("\\/", "/")).strip())
        if not url.startswith("http") or url in seen:
            return
        seen.add(url)
        items.append(make_item(url, title, thumb, source))

    soup = BeautifulSoup(html, "html.parser")
    page_title = soup.title.string.strip() if soup.title and soup.title.string else None

    og_img = soup.find("meta", property="og:image")
    og_thumb = urljoin(page_url, og_img["content"]) if og_img and og_img.get("content") else None

    def heading_for(node):
        h = node.find_previous(["h1", "h2", "h3", "h4"])
        return h.get_text(strip=True) if h else page_title

    for v in soup.find_all("video"):
        poster = urljoin(page_url, v["poster"]) if v.get("poster") else og_thumb
        add(v.get("src"), page_title, poster, "<video>")
        for s in v.find_all("source"):
            add(s.get("src"), page_title, poster, "<source>")

    for a in soup.find_all("audio"):
        add(a.get("src"), page_title, None, "<audio>")
        for s in a.find_all("source"):
            add(s.get("src"), page_title, None, "<source>")

    for prop in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
        for m in soup.find_all("meta", attrs={"property": prop}) + soup.find_all("meta", attrs={"name": prop}):
            add(m.get("content"), page_title, og_thumb, "meta")

    for a in soup.find_all("a", href=True):
        if re.search(r"\." + MEDIA_EXT + r"(?:\?|$)", a["href"], re.I):
            add(a["href"], a.get_text(strip=True) or page_title, og_thumb, "link")

    # YouTube / Vimeo URLs in any URL-bearing attribute (iframes, lightbox links, lazy-load data-*)
    for node in soup.find_all(True):
        for attr in URL_ATTRS:
            val = node.get(attr)
            if val and isinstance(val, str):
                url, thumb = platform_url(val)
                if url:
                    add(url, node.get("title") or heading_for(node), thumb or og_thumb, "embed")

    # ...and anywhere in the raw HTML (inline JSON, scripts)
    for m in YT_RE.finditer(html):
        vid = m.group(1)
        add(f"https://www.youtube.com/watch?v={vid}", page_title,
            f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "page source")
    for m in VIMEO_PLAYER_RE.finditer(html.replace("\\/", "/")):
        url, _ = platform_url(m.group(0))
        add(url, page_title, og_thumb, "page source")

    for m in URL_RE.findall(html):
        add(m, page_title, og_thumb, "page source")

    return items


def render_with_browser(page_url: str):
    """Load the page in headless Chromium so JavaScript-injected players appear.
    Returns (rendered_html | None, [stream URLs seen on the network])."""
    if not HAS_PLAYWRIGHT:
        return None, []
    streams = []

    def on_request(req):
        if STREAM_PATH_RE.search(urlparse(req.url).path):
            streams.append(req.url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox"] if os.environ.get("BROWSER_NO_SANDBOX") == "1" else [],
            )
            page = browser.new_context(user_agent=UA).new_page()
            page.on("request", on_request)
            page.goto(page_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2500)
            rendered = page.content()
            browser.close()
        return rendered, streams
    except Exception:
        return None, []


def scan(page_url: str, referer: str = "", allow_render: bool = True):
    ref = referer or page_url
    meta = {"rendered": False, "hint": None}

    # A pasted stream/file URL needs no discovery
    if is_stream_url(page_url):
        results = [make_item(page_url, None, None, "direct stream")]
        for r in results:
            r["ref"] = ref
        return results, meta

    results = extract_with_ytdlp(page_url, ref)

    html = fetch_html(page_url)
    if html:
        merge(results, parse_html(html, page_url))

    if not results:  # pasted a youtube/vimeo/embed URL that yt-dlp couldn't preview
        embed, thumb = platform_url(page_url)
        if embed:
            results.append(make_item(embed, None, thumb, "embed"))

    if not results and allow_render:
        rendered, streams = render_with_browser(page_url)
        if rendered:
            meta["rendered"] = True
            merge(results, parse_html(rendered, page_url))
        if not results:
            merge(results, [make_item(s, None, None, "network") for s in streams[:5]])

    if not results:
        meta["hint"] = (
            "No videos found in the page HTML. The player is probably injected by JavaScript. "
            + ("" if HAS_PLAYWRIGHT else
               "Install Playwright to render such pages automatically "
               "(pip install playwright && playwright install chromium), or ")
            + "open DevTools > Network, play the video, and paste the .m3u8/.mp4 URL "
              "or the player.vimeo.com embed URL here (set Referer to the page URL)."
        )

    for r in results:
        r["ref"] = ref
    return results, meta


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def find_linked_pages(html: str, page_url: str):
    """Internal links worth scanning, most relevant first: [(url, anchor_text)].

    Priority: links containing the page's last path segment (the course slug), then links
    under the page's own path, then any other same-site link. Capped at MAX_LINKED_PAGES.
    """
    start = urlparse(page_url)
    segments = [s for s in start.path.split("/") if s]
    slug = segments[-1].lower() if segments else ""
    base_path = start.path.rstrip("/")

    soup = BeautifulSoup(html, "html.parser")
    seen = {page_url.split("#")[0].rstrip("/")}
    by_slug, by_prefix, others = [], [], []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(page_url, href).split("#")[0]
        p = urlparse(url)
        if p.scheme not in ("http", "https") or p.netloc != start.netloc:
            continue
        if SKIP_LINK_RE.search(p.path) or url.rstrip("/") in seen:
            continue
        seen.add(url.rstrip("/"))
        entry = (url, " ".join(a.get_text(" ", strip=True).split()))
        path = p.path.lower()
        if slug and slug in path:
            by_slug.append(entry)
        elif base_path and path.startswith(base_path.lower() + "/"):
            by_prefix.append(entry)
        else:
            others.append(entry)

    return (by_slug or by_prefix or others)[:MAX_LINKED_PAGES]


def scan_site(page_url: str, referer: str = ""):
    """Scan a course/index page AND the pages it links to (same site only)."""
    meta = {"rendered": False, "hint": None, "pages_scanned": 0, "pages_empty": 0}

    results, _ = scan(page_url, referer, allow_render=False)
    for v in results:
        v["page"] = page_url

    html = fetch_html(page_url)
    links = find_linked_pages(html, page_url) if html else []
    if len(links) < 2:  # course UIs often build their lesson list with JavaScript
        rendered, _ = render_with_browser(page_url)
        if rendered:
            meta["rendered"] = True
            links = find_linked_pages(rendered, page_url)
    links = [(u, t) for u, t in links if is_safe_url(u)]

    def work(link):
        url, text = link
        try:
            vids, _ = scan(url)  # each lesson uses its own URL as Referer
        except Exception:  # noqa: BLE001
            vids = []
        label = TIME_RE.sub("", text).strip(" -|\u00b7")
        if len(vids) == 1 and label:
            vids[0]["title"] = label
        for v in vids:
            v["page"] = url
        return vids

    with ThreadPoolExecutor(max_workers=3) as pool:
        batches = list(pool.map(work, links))

    for vids in batches:
        meta["pages_scanned"] += 1
        if not vids:
            meta["pages_empty"] += 1
        merge(results, vids)

    if not results:
        meta["hint"] = (
            f"Scanned {meta['pages_scanned']} linked page(s) and found no videos. "
            "Lessons may be members-only, or load their player with JavaScript "
            "(Playwright helps with the latter)."
            if links else
            "No lesson links were found on that page. Try scanning a lesson page directly."
        )
    return results, meta


@app.post("/api/scan")
def api_scan():
    if hit_count("scan:" + client_ip(), 60) >= 20:
        return jsonify(error="Too many scans in a minute, slow down."), 429
    record_hit("scan:" + client_ip())
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    referer = body.get("referer", "").strip()
    if not url:
        return jsonify(error="Please enter a URL."), 400
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not is_safe_url(url) or (referer and not is_safe_url(referer)):
        return jsonify(error="That URL is not allowed or cannot be resolved."), 400
    try:
        if body.get("follow"):
            videos, meta = scan_site(url, referer)
        else:
            videos, meta = scan(url, referer)
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=f"Scan failed: {friendly_error(exc)}"), 500
    return jsonify(videos=videos, count=len(videos), **meta)


@app.get("/download")
def download():
    url = request.args.get("u", "")
    kind = request.args.get("type", "video")
    ref = request.args.get("ref") or url
    if not is_safe_url(url):
        return "Invalid URL", 400
    headers = request_headers(ref)

    # Plain progressive file -> stream it straight through
    if kind == "video" and DIRECT_RE.search(urlparse(url).path + "?"):
        upstream = requests.get(url, headers=headers, stream=True, timeout=20)
        if upstream.status_code >= 400:
            return f"Upstream returned {upstream.status_code}", 502
        out_headers = {"Content-Disposition": f'attachment; filename="{filename_from_url(url)}"'}
        if upstream.headers.get("Content-Length"):
            out_headers["Content-Length"] = upstream.headers["Content-Length"]
        return Response(
            stream_with_context(upstream.iter_content(chunk_size=1 << 16)),
            content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
            headers=out_headers,
        )

    # Everything else (HLS/DASH, embeds, platform pages) -> yt-dlp
    tmp = tempfile.mkdtemp(prefix="vscrape_")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": os.path.join(tmp, "%(title).80s.%(ext)s"),
        "restrictfilenames": True,
        "http_headers": headers,
    }
    if kind == "audio":
        opts["format"] = "bestaudio/best"
        if HAS_FFMPEG:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
    else:
        if HAS_FFMPEG:
            opts["format"] = (
                "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/"   # H.264 + AAC: merges cleanly into mp4 (Vimeo HLS, most sites)
                "bv*[vcodec^=avc1]+ba[ext=m4a]/"
                "b[ext=mp4]/"
                "bv*+ba/b"
            )
            opts["merge_output_format"] = "mp4/mkv"      # mkv accepts any codec combo
        else:
            opts["format"] = "b"                         # single pre-merged file, no ffmpeg needed

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = os.listdir(tmp)
        if not files:
            raise RuntimeError("No file was produced.")
        path = os.path.join(tmp, max(files, key=lambda f: os.path.getsize(os.path.join(tmp, f))))
        resp = send_file(path, as_attachment=True, download_name=os.path.basename(path))
        resp.call_on_close(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return resp
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return f"Download failed: {friendly_error(exc)}", 500


@app.get("/")
def index():
    return render_template_string(PAGE, has_ffmpeg=HAS_FFMPEG, has_playwright=HAS_PLAYWRIGHT)


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Scraper</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --line:#2a2f3a; --text:#e8eaf0; --muted:#8b93a5; --accent:#4f8cff; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  main { max-width:960px; margin:0 auto; padding:40px 20px; }
  h1 { margin:0 0 6px; font-size:26px; }
  p.sub { margin:0 0 24px; color:var(--muted); }
  form .row { display:flex; gap:10px; }
  input[type=text] { flex:1; width:100%; padding:12px 14px; border-radius:10px; border:1px solid var(--line); background:var(--card); color:var(--text); font-size:15px; }
  input[type=text]:focus { outline:2px solid var(--accent); border-color:transparent; }
  details { margin-top:10px; color:var(--muted); font-size:13px; }
  details summary { cursor:pointer; }
  details input { margin-top:8px; }
  label.check { display:block; margin-top:10px; color:var(--muted); font-size:13px; cursor:pointer; }
  label.check input { margin-right:6px; }
  button, a.btn { padding:10px 16px; border:0; border-radius:10px; background:var(--accent); color:#fff; font-weight:600; font-size:14px; cursor:pointer; text-decoration:none; display:inline-block; }
  button:disabled { opacity:.6; cursor:wait; }
  a.btn.alt { background:transparent; border:1px solid var(--line); color:var(--text); }
  #status { margin:18px 0; color:var(--muted); white-space:pre-wrap; }
  #status.error { color:#ff6b6b; }
  #grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; overflow:hidden; display:flex; flex-direction:column; }
  .thumb { aspect-ratio:16/9; background:#0b0d11 center/cover no-repeat; position:relative; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:13px; }
  .dur { position:absolute; right:8px; bottom:8px; background:rgba(0,0,0,.75); padding:2px 6px; border-radius:6px; font-size:12px; }
  .body { padding:14px; display:flex; flex-direction:column; gap:10px; flex:1; }
  .title { font-weight:600; overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; word-break:break-word; }
  .meta { color:var(--muted); font-size:12px; }
  .actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:auto; }
  .actions .btn { padding:8px 12px; font-size:13px; }
</style>
</head>
<body>
<main>
  <h1>Video Scraper</h1>
  <p class="sub">Paste a page URL (or an embed / .m3u8 / .mp4 URL) and get every video found.
    {% if not has_ffmpeg %}<br><em>ffmpeg not found: MP3 extraction and HD merging are limited.</em>{% endif %}
    {% if not has_playwright %}<br><em>Playwright not installed: pages that load players with JavaScript may come back empty.</em>{% endif %}
  </p>
  <form id="form">
    <div class="row">
      <input type="text" id="url" placeholder="https://example.com/page-with-videos" autocomplete="off" required>
      <button id="go" type="submit">Scan</button>
    </div>
    <label class="check"><input type="checkbox" id="follow">Scan linked pages too (course / index pages, up to 20 links on the same site)</label>
    <details>
      <summary>Referer (optional)</summary>
      <input type="text" id="referer" placeholder="Page that embeds the video, e.g. https://course-site.com/lesson/1" autocomplete="off">
    </details>
  </form>
  <div id="status"></div>
  <div id="grid"></div>
</main>

<script>
const form = document.getElementById('form');
const urlInput = document.getElementById('url');
const refInput = document.getElementById('referer');
const followInput = document.getElementById('follow');
const go = document.getElementById('go');
const statusEl = document.getElementById('status');
const grid = document.getElementById('grid');

function el(tag, props = {}, ...children) {
  const n = document.createElement(tag);
  Object.assign(n, props);
  children.forEach(c => n.append(c));
  return n;
}

function dl(v, type) {
  return `/download?u=${encodeURIComponent(v.url)}&type=${type}&ref=${encodeURIComponent(v.ref || '')}`;
}

function card(v) {
  const thumb = el('div', { className: 'thumb' });
  if (v.thumbnail) thumb.style.backgroundImage = `url("${v.thumbnail.replace(/"/g, '%22')}")`;
  else thumb.textContent = 'No preview';
  if (v.duration) thumb.append(el('span', { className: 'dur', textContent: v.duration }));

  const actions = el('div', { className: 'actions' },
    el('a', { className: 'btn', href: dl(v, 'video'), textContent: 'Download video' }),
    el('a', { className: 'btn alt', href: dl(v, 'audio'), textContent: 'Audio' })
  );
  if (v.direct) {
    actions.append(el('a', { className: 'btn alt', href: v.direct, target: '_blank', rel: 'noopener', textContent: 'Direct link' }));
  }

  return el('div', { className: 'card' }, thumb,
    el('div', { className: 'body' },
      el('div', { className: 'title', textContent: v.title, title: v.title }),
      (() => {
        const m = el('div', { className: 'meta', textContent: `via ${v.source}` });
        if (v.page) m.append(' \u00b7 ', el('a', { href: v.page, target: '_blank', rel: 'noopener', textContent: 'source page', style: 'color:inherit' }));
        return m;
      })(),
      actions
    )
  );
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  grid.innerHTML = '';
  statusEl.className = '';
  statusEl.textContent = followInput.checked
    ? 'Scanning the page and its linked pages... this can take a minute.'
    : 'Scanning... (JavaScript-heavy pages can take up to 30 s)';
  go.disabled = true;
  try {
    const res = await fetch('/api/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: urlInput.value, referer: refInput.value, follow: followInput.checked })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Request failed');
    if (!data.count) {
      statusEl.textContent = data.hint || 'No videos found on that page.';
    } else {
      let msg = `Found ${data.count} video${data.count === 1 ? '' : 's'}`;
      if (data.pages_scanned !== undefined) {
        msg += ` across ${data.pages_scanned} linked page${data.pages_scanned === 1 ? '' : 's'}`;
        if (data.pages_empty) msg += ` (${data.pages_empty} had no accessible video, e.g. members-only lessons)`;
      }
      statusEl.textContent = msg + (data.rendered ? ' (page rendered in a browser).' : '.');
      data.videos.forEach(v => grid.append(card(v)));
    }
  } catch (err) {
    statusEl.className = 'error';
    statusEl.textContent = err.message;
  } finally {
    go.disabled = false;
  }
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost"):
        print("WARNING: there is no login; anyone who can reach this port can use the app.")
    app.run(host=host, port=int(os.environ.get("PORT", "5000")), debug=False)
