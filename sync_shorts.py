#!/usr/bin/env python3
"""Sync Javtiful Shorts feed -> shorts.json (public static file for CM FLIX app).

Runs in GitHub Actions on a schedule. No video is downloaded or re-uploaded:
the JSON only stores Javtiful's own stream URLs (played direct from their hosts).

Data source: the JSON feed API at /shorts/feed (cursor-paged). The HTML page
scrape is kept only as a fallback, because bot protection can serve a
challenge page to datacenter runners instead of the real page.

Merge rules:
- New feed items are appended (deduped by uid).
- Existing entries are refreshed with fresh feed data (URLs can change).
- Entries are NEVER deleted by the sync, except uids listed in removed.json
  (manual blocklist: add a uid there to permanently hide a short).
- Feed items whose full-video slug is present but does NOT end with
  '-reducing-mosaic' are skipped (the app only carries Reducing Mosaic).
  Items with no full-video URL are kept (the feed is verified Reducing
  Mosaic only; the app hides the Watch-Full-Video button when the URL
  is missing).
"""
import http.cookiejar
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

BASE = "https://javtiful.com"
SHORTS_PAGE = BASE + "/shorts"
FEED_URL = BASE + "/shorts/feed"
MAX_ITEMS = 500
MAX_FEED_PAGES = 25

BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)

SHORTS_JSON = "shorts.json"
REMOVED_JSON = "removed.json"

# One opener with a cookie jar for the whole run: the feed API ties a feed
# session (cursor) to session cookies. Without persisting cookies, the
# paged request comes back 409 and pagination stops after the first 8.
_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_COOKIE_JAR)
)


def http_get(url, accept):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE + "/",
        },
        method="GET",
    )
    try:
        with _OPENER.open(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} for {url}")
        return e.code, None
    except Exception as e:
        print(f"GET failed {url}: {e}")
        return None, None


def absolutize(url):
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


def pick_media_url(item):
    """Find the playable HLS/MP4 URL inside one feed item, defensively."""
    candidates = []

    def collect(value):
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, dict):
            for key in ("url", "src", "hls", "m3u8", "mp4", "playback", "file"):
                if key in value:
                    collect(value[key])
        elif isinstance(value, list):
            for v in value:
                collect(v)

    for key in ("media", "streamUrl", "stream_url", "videoUrl", "video_url", "url"):
        if key in item:
            collect(item[key])
    # Last resort: any .m3u8/.mp4 URL anywhere in the item JSON.
    if not any(".m3u8" in c or ".mp4" in c for c in candidates):
        blob = json.dumps(item)
        candidates += re.findall(r'https?://[^\s"\'<>]+\.(?:m3u8|mp4)[^\s"\'<>]*', blob)

    for c in candidates:
        if ".m3u8" in c or ".mp4" in c:
            return absolutize(c)
    return ""


def map_item(item):
    if not isinstance(item, dict):
        return None
    uid = str(item.get("uid") or "").strip()
    if not uid:
        return None
    full = absolutize(str(item.get("fullVideoUrl") or item.get("full_video_url") or ""))
    # Full-video URL present but not Reducing Mosaic -> skip.
    # Full-video URL absent -> keep (feed is verified Reducing Mosaic only).
    if full and "-reducing-mosaic" not in full:
        return None
    stream = pick_media_url(item)
    if not stream:
        return None
    actress = item.get("actress") or {}
    if not isinstance(actress, dict):
        actress = {}

    def to_int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "uid": uid,
        "caption": str(item.get("caption") or ""),
        "poster": absolutize(str(item.get("poster") or "")),
        "streamUrl": stream,
        "fullVideoUrl": full,
        "actress": {
            "name": str(actress.get("name") or ""),
            "handle": str(actress.get("handle") or ""),
            "avatar": absolutize(str(actress.get("avatar") or "")),
        },
        "likes": to_int(item.get("likes")),
        "comments": to_int(item.get("comments")),
        "shares": to_int(item.get("shares")),
    }


def parse_config(html):
    """Extract the embedded JSON config block from the /shorts HTML."""
    m = re.search(
        r'<script[^>]*data-short-config[^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def collect_from_feed_payload(data, items, seen):
    """Append mapped items from one feed-API payload. Returns next cursor."""
    if not isinstance(data, dict):
        return ""
    if data.get("success") is False:
        print("Feed API returned success=false")
        return ""
    batch = data.get("items") or data.get("data") or []
    for raw in batch:
        mapped = map_item(raw)
        if mapped and mapped["uid"] not in seen:
            seen.add(mapped["uid"])
            items.append(mapped)
    nxt = data.get("cursor") or data.get("nextCursor") or data.get("next_cursor") or ""
    return str(nxt)


def fetch_via_feed_api():
    """Primary path: /shorts/feed JSON API, cursor-paged."""
    items, seen = [], set()
    status, body = http_get(FEED_URL, "application/json, */*")
    if body is None:
        print("Feed API first page failed")
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print("Feed API first page was not JSON "
              f"(status={status}, bytes={len(body)})")
        return []
    cursor = collect_from_feed_payload(data, items, seen)
    pages = 0
    while cursor and pages < MAX_FEED_PAGES:
        pages += 1
        url = FEED_URL + "?cursor=" + urllib.request.quote(cursor)
        status, body = http_get(url, "application/json, */*")
        if body is None:
            break
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            print(f"Feed page {pages} was not JSON, stopping pagination")
            break
        before = len(items)
        nxt = collect_from_feed_payload(data, items, seen)
        if len(items) == before or not nxt or nxt == cursor:
            break
        cursor = nxt
    print(f"Feed API: {len(items)} shorts ({pages} extra pages)")
    return items


def fetch_via_html():
    """Fallback path: embedded JSON config in the /shorts HTML page."""
    status, html = http_get(SHORTS_PAGE, "text/html,application/xhtml+xml,*/*")
    if html is None:
        return []
    print(f"HTML fallback: status={status}, bytes={len(html)}, "
          f"has_config={'data-short-config' in html}")
    config = parse_config(html)
    if not config:
        print("No embedded shorts config found on page")
        return []
    items, seen = [], set()
    for raw in config.get("items") or []:
        mapped = map_item(raw)
        if mapped and mapped["uid"] not in seen:
            seen.add(mapped["uid"])
            items.append(mapped)
    print(f"HTML fallback: {len(items)} shorts from first page")
    return items


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    existing = load_json(SHORTS_JSON, {"items": []})
    removed = load_json(REMOVED_JSON, [])
    removed_set = set(str(x) for x in removed if isinstance(removed, list))
    by_uid = {}
    for entry in existing.get("items") or []:
        if isinstance(entry, dict) and entry.get("uid"):
            by_uid[str(entry["uid"])] = entry
    fresh = fetch_via_feed_api()
    if not fresh:
        print("Feed API returned nothing, trying HTML fallback")
        fresh = fetch_via_html()
    if not fresh and not by_uid:
        print("Feed empty and no existing data — refusing to write empty file")
        return 1
    for entry in fresh:
        if entry["uid"] in removed_set:
            continue
        by_uid[entry["uid"]] = entry  # refresh wins
    for uid in list(by_uid):
        if uid in removed_set:
            del by_uid[uid]
    # Newest first (feed order), capped.
    order = [e["uid"] for e in fresh if e["uid"] in by_uid]
    for uid in by_uid:
        if uid not in order:
            order.append(uid)
    items = [by_uid[u] for u in order[:MAX_ITEMS]]
    out = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    before = json.dumps(existing, sort_keys=True)
    after = json.dumps(out, sort_keys=True)
    with open(SHORTS_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"Wrote {len(items)} shorts to {SHORTS_JSON} "
          f"(changed={before != after})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
