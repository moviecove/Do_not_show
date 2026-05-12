"""
scraper.py — Scrapes nkiri + 9jarocks and saves to PostgreSQL
Runs continuously on Render as a background worker.

INSTALL:
    pip install requests beautifulsoup4 sqlalchemy psycopg2-binary

RUN:
    python scraper.py
"""

import json, html, time, sys, re, signal
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError

from database import init_db, SessionLocal, Movie, Series, now

# ── Config ────────────────────────────────────────────────────────────────────
DELAY_PAGE   = 3.0
DELAY_MOVIE  = 2.0
MAX_RETRIES  = 4
LOOP_FOREVER = True    # keeps re-scraping for new content after finishing all pages

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Sources ───────────────────────────────────────────────────────────────────
NKIRI_BASE = "https://thenkiri.com.ng"
NKIRI_CATEGORIES = [
    ("Hollywood",       "category/international-movies"),
    ("Nollywood",       "category/nollywood-movies"),
    ("K-Drama",         "category/k-drama"),
    ("TV Series",       "category/tv-series"),
    ("Chinese Drama",   "category/chinese-drama"),
    ("Thai Drama",      "category/thai-drama"),
    ("Animation/Anime", "category/animation"),
    ("Bollywood",       "category/bollywood-movies"),
]

JAROCKS_BASE = "https://www.9jarocks.net"
JAROCKS_CATEGORIES = [
    ("Hollywood",  "category/hollywood-movies"),
    ("Nollywood",  "category/nollywood-movies"),
    ("TV Series",  "category/tv-series"),
    ("Animation",  "category/animation"),
    ("Bollywood",  "category/bollywood"),
]

DIRECT_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".zip", ".rar")
FILE_HOSTS = [
    "gofile.io", "mediafire.com", "drive.google.com", "mega.nz",
    "buzzheavier.com", "pixeldrain.com", "1fichier.com", "rapidgator.net",
    "streamtape.com", "doodstream.com", "mp4upload.com", "mixdrop.co",
    "sendcm.com", "filemoon.", "clicknupload.", "fastdl.", "hubcloud.",
    "gdtot.", "drivebot.", "gdflix.", "uploadhaven.com", "upstream.to",
]

_shutdown = False

def _sig(sig, frame):
    global _shutdown
    print("\n[!] Shutting down gracefully...")
    _shutdown = True

signal.signal(signal.SIGINT,  _sig)
signal.signal(signal.SIGTERM, _sig)

# ── HTTP ──────────────────────────────────────────────────────────────────────
_session = requests.Session()
_session.headers.update(HEADERS)

def http_get(url, referer=""):
    hdrs = {"Referer": referer} if referer else {}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _session.get(url, headers=hdrs, timeout=25, allow_redirects=True)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                wait = 15 * attempt
                print(f"    [!] 429 rate-limited — sleeping {wait}s")
                time.sleep(wait)
                continue
            print(f"    [!] HTTP {r.status_code}: {url}")
            return None
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"    [!] Failed: {url} — {e}")
                return None
            time.sleep(5 * attempt)
    return None

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_direct(url):
    return any(urlparse(url).path.lower().endswith(e) for e in DIRECT_EXTENSIONS)

def is_file_host(url):
    return any(h in url for h in FILE_HOSTS)

def is_downloadwella(url):
    return "downloadwella.com" in url

def quality_label(url, label=""):
    combined = (url + " " + label).lower()
    for q in ["2160p", "4k", "1080p", "720p", "480p", "360p"]:
        if q in combined:
            return q
    return label.strip() or "download"

def clean_title(raw):
    t = re.sub(
        r"\s*\|\s*(Download|Nkiri|9jarocks|Hollywood|Nollywood|Bollywood|Korean|K-Drama).*",
        "", raw, flags=re.IGNORECASE
    ).strip()
    t = re.sub(
        r"\s*(Download\s+)?(Hollywood|Nollywood|Bollywood|Korean|Foreign)\s+Movie$",
        "", t, flags=re.IGNORECASE
    ).strip()
    t = re.sub(r"(\(\d{4}\))(\s*\(\d{4}\))+", r"\1", t).strip()
    return t

def looks_like_series(title, soup):
    if re.search(r"\b(season|episode|series|s\d{2}e\d{2}|complete)\b", title, re.I):
        return True
    if soup.get_text(" ", strip=True).lower().count("episode") >= 3:
        return True
    return False

# ── Download button extractor ─────────────────────────────────────────────────
def extract_buttons(soup):
    links = []
    # data-attributes (nkiri style)
    for div in soup.find_all("div", attrs={"data-attributes": True}):
        raw = div.get("data-attributes", "")
        try:
            decoded = html.unescape(raw)
            data    = json.loads(decoded)
            url     = data.get("url", "").strip()
            label   = data.get("text", "").strip() or div.get_text(strip=True)
            if url and url.startswith("http"):
                links.append((label, url))
        except Exception:
            m = re.search(r'"url"\s*:\s*"([^"]+)"', html.unescape(raw))
            if m:
                url = m.group(1).replace("\\/", "/")
                if url.startswith("http"):
                    links.append((div.get_text(strip=True), url))

    # plain <a> fallback (9jarocks style)
    if not links:
        content = soup.find("div", class_=re.compile(
            r"entry-content|post-content|article-content|tdb-block-inner", re.I))
        if content:
            for a in content.find_all("a", href=True):
                href  = a["href"].strip()
                label = a.get_text(strip=True)
                if href.startswith("http") and (
                    is_direct(href) or is_file_host(href) or is_downloadwella(href)
                ):
                    links.append((label, href))
    return links

# ── XFileSharing resolver ─────────────────────────────────────────────────────
def resolve_xfilesharing(file_page_url, referer):
    parsed  = urlparse(file_page_url)
    file_id = parsed.path.strip("/").split("/")[0]
    base    = f"{parsed.scheme}://{parsed.netloc}"
    post_h  = {
        **HEADERS,
        "Referer":      referer,
        "Origin":       base,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        r1 = requests.post(
            f"{base}/",
            data={"op": "download1", "id": file_id, "referer": "",
                  "method_free": "", "method_premium": ""},
            headers=post_h, timeout=25, allow_redirects=True
        )
        if r1.status_code != 200:
            return None

        soup1     = BeautifulSoup(r1.text, "html.parser")
        form_data = {"op": "download2", "id": file_id, "method_free": ""}
        for inp in soup1.find_all("input"):
            n, v = inp.get("name",""), inp.get("value","")
            if n and n != "method_premium":
                form_data[n] = v

        wait = 4
        cd = soup1.find(id=re.compile("countdown", re.I))
        if cd:
            m = re.search(r"(\d+)", cd.get_text())
            if m:
                wait = min(int(m.group(1)) + 1, 15)
        time.sleep(wait)

        r2 = requests.post(
            f"{base}/", data=form_data,
            headers={**post_h, "Referer": file_page_url},
            timeout=25, allow_redirects=True
        )
        if r2.status_code != 200:
            return None
        if is_direct(r2.url):
            return r2.url

        soup2 = BeautifulSoup(r2.text, "html.parser")
        for a in soup2.find_all("a", href=True):
            full = a["href"] if a["href"].startswith("http") else urljoin(file_page_url, a["href"])
            if is_direct(full):
                return full
        for script in soup2.find_all("script"):
            raw  = script.string or ""
            hits = re.findall(
                r'(https?://[^\s"\'<>]+\.(?:mp4|mkv|avi|mov)[^\s"\'<>]*)', raw, re.I)
            if hits:
                return hits[0]
        hits = re.findall(
            r'https?://[^\s"\'<>]+\.(?:mp4|mkv|avi|mov)[^\s"\'<>]*', r2.text, re.I)
        if hits:
            return hits[0]
        return None
    except Exception as e:
        print(f"      [xfs] Error: {e}")
        return None

# ── Resolve single link ───────────────────────────────────────────────────────
def resolve_link(label, href, referer):
    if is_direct(href):
        return {"label": quality_label(href, label), "url": href, "status": "active"}
    if is_downloadwella(href):
        direct = resolve_xfilesharing(href, referer)
        if direct:
            return {"label": quality_label(direct, label), "url": direct, "status": "active"}
        return {"label": label or "download", "url": href, "status": "unresolved"}
    if is_file_host(href):
        return {"label": quality_label(href, label), "url": href, "status": "active"}
    try:
        head  = requests.head(href, headers={**HEADERS, "Referer": referer},
                              timeout=12, allow_redirects=True)
        final = head.url
        ct    = head.headers.get("Content-Type", "")
        if is_direct(final) or any(x in ct for x in ["video/", "application/octet"]):
            return {"label": quality_label(final, label), "url": final, "status": "active"}
        if is_downloadwella(final):
            direct = resolve_xfilesharing(final, referer=href)
            if direct:
                return {"label": quality_label(direct, label), "url": direct, "status": "active"}
    except Exception:
        pass
    return {"label": label or "download", "url": href, "status": "active"}

# ── Meta helpers ──────────────────────────────────────────────────────────────
def extract_poster(soup):
    for tag, attrs, key in [
        ("meta", {"property": "og:image"}, "content"),
        ("meta", {"name": "twitter:image"}, "content"),
    ]:
        t = soup.find(tag, attrs)
        if t and t.get(key):
            return t[key]
    content = soup.find("div", class_=re.compile(
        r"entry-content|post-content|article-content|tdb-block-inner", re.I))
    if content:
        for img in content.find_all("img", src=True):
            src = img.get("src") or img.get("data-src", "")
            if src and "icon" not in src.lower() and "logo" not in src.lower():
                return src
    return ""

def extract_description(soup):
    for tag, attrs, key in [
        ("meta", {"property": "og:description"}, "content"),
        ("meta", {"name": "description"}, "content"),
    ]:
        t = soup.find(tag, attrs)
        if t and t.get(key):
            return t[key].strip()
    content = soup.find("div", class_=re.compile(
        r"entry-content|post-content|article-content|tdb-block-inner", re.I))
    if content:
        paras = [p.get_text(strip=True) for p in content.find_all("p")
                 if len(p.get_text(strip=True)) > 40][:3]
        if paras:
            return " ".join(paras)
    return ""

# ── Season/episode extractor ──────────────────────────────────────────────────
def extract_seasons(soup, page_url):
    seasons = []
    content = soup.find("div", class_=re.compile(
        r"entry-content|post-content|article-content|tdb-block-inner", re.I)) or soup

    season_headings = content.find_all(
        re.compile(r"^h[2-4]$"),
        string=re.compile(r"\bseason\s*\d+|s\d{2}\b", re.IGNORECASE)
    )

    if season_headings:
        for sh in season_headings:
            season_name = sh.get_text(strip=True)
            eps = []
            for sib in sh.find_next_siblings():
                if sib.name and re.match(r"^h[2-4]$", sib.name):
                    break
                for div in sib.find_all("div", attrs={"data-attributes": True}):
                    raw = div.get("data-attributes", "")
                    try:
                        data = json.loads(html.unescape(raw))
                        url  = data.get("url", "").strip()
                        lbl  = data.get("text", "").strip()
                        if url and url.startswith("http"):
                            ep_m = re.search(r"(episode\s*\d+|ep\s*\d+|e\d{2,})", lbl, re.I)
                            eps.append({
                                "episode": ep_m.group(1) if ep_m else lbl or f"Ep {len(eps)+1}",
                                "raw_url": url, "label": lbl
                            })
                    except Exception:
                        pass
                for a in sib.find_all("a", href=True):
                    href  = a["href"].strip()
                    label = a.get_text(strip=True)
                    if href.startswith("http") and (
                        is_direct(href) or is_file_host(href) or is_downloadwella(href)
                    ):
                        ep_m = re.search(r"(episode\s*\d+|ep\s*\d+|e\d{2,})", label, re.I)
                        eps.append({
                            "episode": ep_m.group(1) if ep_m else label or f"Ep {len(eps)+1}",
                            "raw_url": href, "label": label
                        })
            if eps:
                seasons.append({"season": season_name, "episodes": eps})

    if not seasons:
        raw_links = extract_buttons(soup)
        if raw_links:
            eps = []
            for i, (label, href) in enumerate(raw_links, 1):
                ep_m = re.search(r"(episode\s*\d+|ep\s*\d+|e\d{2,})", label, re.I)
                eps.append({
                    "episode": ep_m.group(1) if ep_m else f"Episode {i}",
                    "raw_url": href, "label": label
                })
            seasons = [{"season": "Season 1", "episodes": eps}]

    # Resolve links
    resolved = []
    for season in seasons:
        r_eps = []
        for ep in season["episodes"]:
            dl = resolve_link(ep["label"], ep["raw_url"], page_url)
            r_eps.append({"episode": ep["episode"], "links": [dl]})
            time.sleep(0.4)
        resolved.append({"season": season["season"], "episodes": r_eps})
    return resolved

# ── Scrape one page ───────────────────────────────────────────────────────────
def scrape_page(page_url, category, source_site):
    r = http_get(page_url, referer=page_url)
    if not r:
        return None

    soup  = BeautifulSoup(r.text, "html.parser")
    h1    = soup.find("h1")
    if not h1:
        return None

    title = clean_title(h1.get_text(strip=True))
    if not title:
        return None

    year_m = re.search(r"\((\d{4})\)", title)
    year   = year_m.group(1) if year_m else (
        re.search(r"/(\d{4})/", page_url) or type("", (), {"group": lambda s, x: ""})()
    ).group(1)

    poster      = extract_poster(soup)
    description = extract_description(soup)
    description = re.sub(r"^\[[\d.\s]+(?:MB|GB)\]\s*[^.]+\.\s*", "", description).strip()

    text     = soup.get_text(" ", strip=True)
    size_m   = re.search(r"\[([\d.]+\s*(?:MB|GB))\]", text)
    filesize = size_m.group(1) if size_m else ""

    tags  = [a.get_text(strip=True) for a in soup.find_all("a", rel=lambda r: r and "tag" in r)]
    genre = ", ".join(tags)

    is_series = looks_like_series(title, soup)

    if is_series:
        seasons = extract_seasons(soup, page_url)
        return {
            "type":        "series",
            "title":       title,
            "year":        year or "",
            "genre":       genre,
            "category":    category,
            "description": description,
            "poster":      poster,
            "page_url":    page_url,
            "source_site": source_site,
            "seasons":     seasons,
        }
    else:
        raw_links      = extract_buttons(soup)
        download_links = []
        seen           = set()
        for label, href in raw_links:
            if href in seen:
                continue
            seen.add(href)
            dl = resolve_link(label, href, page_url)
            download_links.append(dl)

        return {
            "type":           "movie",
            "title":          title,
            "year":           year or "",
            "genre":          genre,
            "category":       category,
            "description":    description,
            "filesize":       filesize,
            "poster":         poster,
            "page_url":       page_url,
            "source_site":    source_site,
            "download_links": download_links,
        }

# ── Save to DB ────────────────────────────────────────────────────────────────
def save_item(item):
    db = SessionLocal()
    try:
        if item["type"] == "series":
            obj = Series(
                title       = item["title"],
                year        = item["year"],
                genre       = item["genre"],
                category    = item["category"],
                description = item["description"],
                poster      = item["poster"],
                page_url    = item["page_url"],
                source_site = item["source_site"],
                seasons     = item["seasons"],
            )
        else:
            obj = Movie(
                title          = item["title"],
                year           = item["year"],
                genre          = item["genre"],
                category       = item["category"],
                description    = item["description"],
                filesize       = item.get("filesize", ""),
                poster         = item["poster"],
                page_url       = item["page_url"],
                source_site    = item["source_site"],
                download_links = item["download_links"],
            )
        db.add(obj)
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False  # already exists
    except Exception as e:
        db.rollback()
        print(f"    [db] Save error: {e}")
        return False
    finally:
        db.close()

def already_scraped(page_url):
    db = SessionLocal()
    try:
        m = db.query(Movie).filter(Movie.page_url == page_url).first()
        s = db.query(Series).filter(Series.page_url == page_url).first()
        return m is not None or s is not None
    finally:
        db.close()

# ── Category page link getter ─────────────────────────────────────────────────
NKIRI_SKIP = [
    "/category/", "/tag/", "/author/", "/page/",
    "/login", "/wp-", "javascript:", "#",
    "facebook.com", "twitter.com", "themeruby.com",
]
JAROCKS_SKIP = [
    "/category/", "/tag/", "/author/", "/page/",
    "/login", "/wp-", "javascript:", "#",
    "facebook.com", "twitter.com",
]

def get_page_links(base_url, category_path, page, skip_patterns):
    url = (
        f"{base_url}/{category_path}/"
        if page == 1
        else f"{base_url}/{category_path}/page/{page}/"
    )
    r = http_get(url)
    if not r:
        return []

    soup  = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith(base_url + "/"):
            continue
        if any(s in href for s in skip_patterns):
            continue
        if href.rstrip("/") == base_url:
            continue
        slug = href.replace(base_url, "").strip("/")
        if len(slug) < 5:
            continue
        links.append(href)

    return list(dict.fromkeys(links))

# ── Sitemap ───────────────────────────────────────────────────────────────────
def get_sitemap_urls(base_url, max_urls=50000):
    discovered = []
    tried      = set()

    def fetch(sitemap_url):
        if sitemap_url in tried or len(discovered) >= max_urls:
            return
        tried.add(sitemap_url)
        r = http_get(sitemap_url)
        if not r:
            return
        soup = BeautifulSoup(r.text, "xml")
        for loc in soup.find_all("sitemap"):
            inner = loc.find("loc")
            if inner:
                fetch(inner.get_text(strip=True))
        for loc in soup.find_all("url"):
            inner = loc.find("loc")
            if inner:
                u   = inner.get_text(strip=True)
                rel = u.replace(base_url, "").strip("/")
                if (
                    u.startswith(base_url + "/")
                    and len(rel) > 5
                    and "/category/" not in rel
                    and "/tag/"      not in rel
                    and "/author/"   not in rel
                    and "/page/"     not in rel
                ):
                    discovered.append(u)
        time.sleep(1)

    for path in ["/sitemap.xml", "/sitemap_index.xml", "/post-sitemap.xml"]:
        fetch(base_url + path)

    print(f"  [sitemap:{base_url}] Found {len(discovered)} URLs")
    return list(dict.fromkeys(discovered))

# ── Scrape one source ─────────────────────────────────────────────────────────
def scrape_source(base_url, categories, skip_patterns, source_name, counts):
    print(f"\n{'='*50}")
    print(f"  Source: {source_name} ({base_url})")
    print(f"{'='*50}")

    # Sitemap first
    print(f"\n[*] Fetching sitemap for {source_name}...")
    sitemap_urls = get_sitemap_urls(base_url)
    for url in sitemap_urls:
        if _shutdown:
            return
        if already_scraped(url):
            continue
        item = scrape_page(url, "Unknown", source_name)
        if item:
            saved = save_item(item)
            if saved:
                t = item["type"]
                counts[t] = counts.get(t, 0) + 1
                print(
                    f"  [{source_name}][{t[0].upper()}:{counts[t]:04d}] "
                    f"{item['title']} ({item['year']})"
                )
        time.sleep(DELAY_MOVIE)

    # Category pages
    for cat_name, cat_path in categories:
        if _shutdown:
            return
        print(f"\n  [{source_name}] == {cat_name} ==")
        page          = 1
        empty_strikes = 0

        while not _shutdown:
            links = get_page_links(base_url, cat_path, page, skip_patterns)
            if not links:
                empty_strikes += 1
                if empty_strikes >= 2:
                    break
                page += 1
                time.sleep(DELAY_PAGE)
                continue

            empty_strikes = 0
            for url in links:
                if _shutdown:
                    return
                if already_scraped(url):
                    continue
                item = scrape_page(url, cat_name, source_name)
                if item:
                    saved = save_item(item)
                    if saved:
                        t = item["type"]
                        counts[t] = counts.get(t, 0) + 1
                        print(
                            f"  [{source_name}][{t[0].upper()}:{counts[t]:04d}] "
                            f"{item['title']} ({item['year']})"
                        )
                time.sleep(DELAY_MOVIE)

            page += 1
            time.sleep(DELAY_PAGE)

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    print("[*] Initializing database...")
    init_db()

    counts = {"movie": 0, "series": 0}
    run    = 0

    while not _shutdown:
        run += 1
        print(f"\n[*] ── Scrape Run #{run} ── {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        scrape_source(
            NKIRI_BASE, NKIRI_CATEGORIES, NKIRI_SKIP, "nkiri", counts
        )
        if not _shutdown:
            scrape_source(
                JAROCKS_BASE, JAROCKS_CATEGORIES, JAROCKS_SKIP, "9jarocks", counts
            )

        if not LOOP_FOREVER:
            break

        print(f"\n[*] Run #{run} complete. Movies:{counts['movie']} Series:{counts['series']}")
        print(f"[*] Sleeping 30 mins before next run...")
        for _ in range(1800):  # 30 mins
            if _shutdown:
                break
            time.sleep(1)

    print(f"\n[ok] Scraper stopped. Total — Movies:{counts['movie']} Series:{counts['series']}")


if __name__ == "__main__":
    main()
