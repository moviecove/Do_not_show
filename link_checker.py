"""
link_checker.py — Auto-refreshes dead download links every 6 hours
Runs as a Render Cron Job:  0 */6 * * *  python link_checker.py

Checks every link in DB, marks dead ones, re-scrapes fresh links from source page.
"""

import time, re, requests
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from database import init_db, SessionLocal, Movie, Series, now

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
}

DIRECT_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".wmv")
DELAY = 1.0  # seconds between checks

# ── Import resolve helpers from scraper ──────────────────────────────────────
from scraper import (
    extract_buttons, resolve_link, http_get,
    is_direct, is_file_host, is_downloadwella
)


def check_link(url):
    """
    Returns True if link is alive, False if dead.
    Uses HEAD request — fast, doesn't download the file.
    """
    try:
        r = requests.head(
            url, headers=HEADERS, timeout=15,
            allow_redirects=True
        )
        if r.status_code in (200, 206):
            return True
        if r.status_code in (403, 401):
            # 403 on file hosts often means link exists but needs browser
            # treat as alive unless it's a known dead pattern
            ct = r.headers.get("Content-Type", "")
            if "video" in ct or "octet" in ct:
                return True
            return False
        if r.status_code in (404, 410, 400):
            return False
        # 5xx — assume alive for now
        return True
    except Exception:
        return False


def refresh_movie_links(movie, db):
    """Re-scrape the movie page and update links in DB."""
    print(f"  [refresh] {movie.title} — re-scraping {movie.page_url}")
    r = http_get(movie.page_url)
    if not r:
        print(f"  [refresh] Page unreachable: {movie.page_url}")
        return

    soup      = BeautifulSoup(r.text, "html.parser")
    raw_links = extract_buttons(soup)
    if not raw_links:
        print(f"  [refresh] No buttons found on page")
        return

    new_links = []
    seen      = set()
    for label, href in raw_links:
        if href in seen:
            continue
        seen.add(href)
        dl = resolve_link(label, href, movie.page_url)
        new_links.append(dl)
        time.sleep(0.5)

    if new_links:
        movie.download_links = new_links
        movie.link_checked_at = now()
        movie.updated_at      = now()
        db.commit()
        print(f"  [refresh] Updated {len(new_links)} links for: {movie.title}")


def refresh_series_links(series, db):
    """Re-scrape series page and update episode links."""
    print(f"  [refresh] {series.title} — re-scraping")
    from scraper import extract_seasons
    r = http_get(series.page_url)
    if not r:
        return

    soup    = BeautifulSoup(r.text, "html.parser")
    seasons = extract_seasons(soup, series.page_url)
    if seasons:
        series.seasons    = seasons
        series.updated_at = now()
        db.commit()
        print(f"  [refresh] Updated {len(seasons)} seasons for: {series.title}")


def run_checker():
    print(f"\n[link_checker] Starting — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    init_db()
    db = SessionLocal()

    stats = {
        "movies_checked":   0,
        "movies_dead":      0,
        "movies_refreshed": 0,
        "series_checked":   0,
    }

    try:
        # ── Check movies ──────────────────────────────────────────────────────
        movies = db.query(Movie).filter(Movie.is_active == True).all()
        print(f"[link_checker] Checking {len(movies)} movies...")

        for movie in movies:
            if not movie.download_links:
                continue

            dead_count  = 0
            total_links = len(movie.download_links)

            for dl in movie.download_links:
                url = dl.get("url", "")
                if not url:
                    continue

                alive = check_link(url)
                dl["status"] = "active" if alive else "dead"

                if not alive:
                    dead_count += 1
                    print(f"  [dead] {movie.title[:40]} -> {url[:60]}")

                stats["movies_checked"] += 1
                time.sleep(DELAY)

            # If more than half links are dead — refresh from source page
            if dead_count > 0 and dead_count >= total_links / 2:
                stats["movies_dead"] += 1
                refresh_movie_links(movie, db)
                stats["movies_refreshed"] += 1
            else:
                # Just update statuses
                movie.download_links  = movie.download_links
                movie.link_checked_at = now()
                db.commit()

        # ── Check series episode links ────────────────────────────────────────
        all_series = db.query(Series).filter(Series.is_active == True).all()
        print(f"\n[link_checker] Checking {len(all_series)} series...")

        for series in all_series:
            dead_count  = 0
            total_links = 0

            for season in (series.seasons or []):
                for ep in season.get("episodes", []):
                    for dl in ep.get("links", []):
                        url = dl.get("url", "")
                        if not url:
                            continue
                        total_links += 1
                        alive = check_link(url)
                        dl["status"] = "active" if alive else "dead"
                        if not alive:
                            dead_count += 1
                        stats["series_checked"] += 1
                        time.sleep(DELAY)

            if dead_count > 0 and total_links > 0 and dead_count >= total_links / 2:
                refresh_series_links(series, db)
            else:
                series.seasons    = series.seasons
                series.updated_at = now()
                db.commit()

    finally:
        db.close()

    print(f"\n[link_checker] Done!")
    print(f"  Movies checked:   {stats['movies_checked']}")
    print(f"  Dead links found: {stats['movies_dead']}")
    print(f"  Pages refreshed:  {stats['movies_refreshed']}")
    print(f"  Series checked:   {stats['series_checked']}")


if __name__ == "__main__":
    run_checker()
