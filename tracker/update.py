#!/usr/bin/env python3
"""Janet Knott Globe byline tracker.

Searches Google News for fresh Boston Globe articles that credit Janet Knott,
appends any new ones to tracker/sightings.json, and regenerates /tracker.html.

Detection is best-effort by design: byline credits usually live in photo
captions that search engines do not index, so this catches the cases where
"Janet Knott" appears in indexed text and misses caption-only credits. The
JSON datastore can always be hand-edited to add or correct entries.

For each sighting it also tries to resolve the real Globe URL (the feed only
gives Google News redirect links) and pull the article's social-preview image
to show as an attributed thumbnail. That resolution is brittle (it depends on
an undocumented Google endpoint); when it fails the entry stays link-only and
is retried on the next run.

Stdlib only, so the GitHub Action needs no `pip install`.
"""

import hashlib
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIGHTINGS_PATH = ROOT / "tracker" / "sightings.json"
OUTPUT_PATH = ROOT / "tracker.html"
STYLE_PATH = ROOT / "style.css"

# Google News RSS search for the exact phrase. Filtering to the Globe and to
# the exact name happens below; this just casts the net.
FEED_URL = (
    "https://news.google.com/rss/search"
    "?q=%22Janet+Knott%22&hl=en-US&gl=US&ceid=US:en"
)
USER_AGENT = "janetknott-tracker/1.0 (+https://janetknott.com)"
# A browser UA is needed to resolve Google News links and fetch Globe pages.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
BATCHEXECUTE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

# Janet is a *former* Globe staff photographer, so any reasonably recent Globe
# article carrying her byline is necessarily reusing an old archive photo, i.e.
# a genuine "resurfacing." We keep articles published within this many years and
# drop the originals from her active-staff era. Set to 6 (not 3) so the window
# reaches back to the January 2021 Challenger-anniversary retrospective; lower it
# to tighten the tracker to only the most recent reappearances.
RECENT_WINDOW_YEARS = 6

# A 4-digit year (1900-2099) sitting near an archive/file cue, so we only claim
# a shoot year when the text actually implies a reused-archive photo.
SHOOT_YEAR_RE = re.compile(
    r"(?:file|archive|globe staff|globe file)[^.]{0,40}?\b((?:19|20)\d{2})\b"
    r"|\b((?:19|20)\d{2})\b[^.]{0,40}?(?:file|archive|globe staff|globe file)",
    re.IGNORECASE,
)


def fetch_feed(url=FEED_URL):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def http_get(url, data=None):
    headers = {"User-Agent": BROWSER_UA}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def resolve_article_url(google_url):
    """Resolve a Google News redirect link to the real publisher URL.

    Google News no longer 301-redirects; the link returns a JS interstitial
    carrying a signature + timestamp that must be POSTed to an internal
    batchexecute endpoint to get the article URL back. This is inherently
    brittle (Google can change it); callers must tolerate a None return.
    """
    html = http_get(google_url)
    sg = re.search(r'data-n-a-sg="([^"]+)"', html)
    ts = re.search(r'data-n-a-ts="([^"]+)"', html)
    aid = re.search(r'data-n-a-id="([^"]+)"', html)
    if not (sg and ts and aid):
        return None

    inner = [
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
          None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1,
         None, 0, 0, None, 0],
        aid.group(1), int(ts.group(1)), sg.group(1),
    ]
    payload = "f.req=" + urllib.parse.quote(
        json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])
    )
    resp = http_get(BATCHEXECUTE_URL, payload.encode())
    if resp.startswith(")]}'"):
        resp = resp[resp.find("\n") + 1:]
    for row in json.loads(resp):
        if len(row) > 2 and row[0] == "wrb.fr" and row[2]:
            parsed = json.loads(row[2])
            if isinstance(parsed, list) and len(parsed) > 1 \
                    and isinstance(parsed[1], str) and parsed[1].startswith("http"):
                return parsed[1]
    return None


OG_IMAGE_PATTERNS = (
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
)


def extract_og_image(html):
    """Return the article's social-preview image URL, or None."""
    for pat in OG_IMAGE_PATTERNS:
        m = re.search(pat, html, re.I)
        if m:
            return unescape(m.group(1))
    return None


# A 4-digit year sitting just before a "Janet Knott" photo credit. Globe photo
# captions read like "...the 1979 opener. (Janet Knott/Globe Staff)" or
# "...on Jan. 28, 1986. Janet Knott/Globe Staff", so the year nearest the credit
# is the year the photo was taken. The [^0-9]{0,40} gap stops at any other digit,
# so we only take a year that is genuinely adjacent to her byline.
CAPTION_YEAR_RE = re.compile(
    r"((?:19|20)\d{2})[^0-9]{0,40}?\(?\s*Janet Knott", re.IGNORECASE
)


def detect_shoot_year_from_article(html):
    """Best-effort original-photo year from the caption next to her credit."""
    text = unescape(strip_tags(html))
    years = [int(y) for y in CAPTION_YEAR_RE.findall(text)]
    if not years:
        return None
    # Most frequent (a gallery repeats the same year), tie-broken by earliest.
    counts = Counter(years)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def enrich(sighting):
    """Best-effort: resolve the real Globe URL, preview image, and photo year.

    Failures are non-fatal; the entry stays link-only and is retried next run.
    On a successful article fetch we set "enriched" so we don't re-fetch an
    entry whose photo year is genuinely unknown (e.g. caption says "date
    unknown").
    """
    label = sighting["title"][:45]
    try:
        real = resolve_article_url(sighting["url"])
        if not real:
            print(f"  ! could not resolve {label}")
            return
        sighting["article_url"] = real
        html = http_get(real)

        img = extract_og_image(html)
        if img:
            sighting["image"] = img
        if not sighting.get("shoot_year"):
            year = detect_shoot_year_from_article(html)
            if year:
                sighting["shoot_year"] = year
        sighting["enriched"] = True
        print(f"  * enriched {label} (image={bool(img)}, "
              f"shoot_year={sighting.get('shoot_year')})")
    except Exception as exc:
        print(f"  ! enrich failed for {label}: {exc!r}")


def strip_tags(text):
    return re.sub(r"<[^>]+>", " ", text or "")


def parse_items(xml_bytes, since_year):
    """Yield recent Globe items that credit Janet Knott, as raw dicts."""
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description_raw = item.findtext("description") or ""
        description = unescape(strip_tags(description_raw)).strip()
        pub_raw = item.findtext("pubDate") or ""

        source_el = item.find("source")
        source_name = (source_el.text or "").strip() if source_el is not None else ""
        source_url = source_el.get("url", "") if source_el is not None else ""

        # Filter to the Boston Globe. The feed query is the exact phrase
        # "Janet Knott", so Google has already matched her name inside the
        # article (usually the photo credit). We do NOT re-check the title or
        # description, because the RSS snippet carries only the headline and
        # source name, not the body where the credit lives. Re-checking would
        # drop every real sighting. Non-Globe noise (obituaries, other people
        # named Janet Knott) lives on other domains and is screened out here.
        is_globe = "bostonglobe.com" in source_url.lower() or (
            "boston globe" in source_name.lower()
        )
        if not is_globe:
            continue

        published = ""
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw).date().isoformat()
            except (TypeError, ValueError):
                published = ""

        # Recent-reuses-only: must have a date and fall within the window. Undated
        # items can't be confirmed recent, so they're skipped.
        if not published or int(published[:4]) < since_year:
            continue

        # Google News titles end with " - <Source>"; drop it, the source is known.
        clean_title = unescape(title)
        if source_name and clean_title.endswith(f" - {source_name}"):
            clean_title = clean_title[: -len(f" - {source_name}")].strip()

        yield {
            "url": link,
            "title": clean_title,
            "published": published,
            "source": source_name or "The Boston Globe",
            "snippet": description,
        }


def detect_shoot_year(*texts):
    for text in texts:
        if not text:
            continue
        m = SHOOT_YEAR_RE.search(text)
        if m:
            return int(m.group(1) or m.group(2))
    return None


def normalize_url(url):
    return (url or "").split("#")[0].rstrip("/").lower()


def load_sightings():
    if not SIGHTINGS_PATH.exists():
        return []
    try:
        return json.loads(SIGHTINGS_PATH.read_text() or "[]")
    except json.JSONDecodeError:
        return []


def merge(existing, found, today):
    seen = {normalize_url(s.get("url")) for s in existing}
    added = []
    for item in found:
        key = normalize_url(item["url"])
        if not key or key in seen:
            continue
        seen.add(key)
        added.append(
            {
                "url": item["url"],
                "title": item["title"],
                "published": item["published"],
                "discovered": today,
                "source": item["source"],
                "shoot_year": detect_shoot_year(item["title"], item["snippet"]),
                "snippet": item["snippet"],
            }
        )
    # Newest first, by reuse date (published, falling back to discovered).
    combined = added + existing
    combined.sort(key=lambda s: s.get("published") or s.get("discovered") or "", reverse=True)
    return combined, added


def reuse_year(s):
    stamp = s.get("published") or s.get("discovered") or ""
    return stamp[:4] if len(stamp) >= 4 else ""


def render(sightings, generated_on, style_version=""):
    count = len(sightings)

    if count:
        items = "\n".join(_item_html(s) for s in sightings)
        body = (
            f'      <p class="tracker-count"><strong>{count}</strong> '
            f'Globe sighting{"s" if count != 1 else ""} so far.</p>\n'
            f'      <ul class="tracker-list">\n{items}\n      </ul>\n'
        )
    else:
        body = (
            '      <div class="empty-state">\n'
            "        <p>No sightings logged yet. This tracker watches for Boston Globe\n"
            "        articles that resurface one of Janet&rsquo;s archive photos and credit\n"
            "        her byline. When one turns up, it lands here.</p>\n"
            "      </div>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="description" content="A tracker of Janet Knott's Boston Globe archive photos resurfacing, years later." />
  <title>Globe Sightings | Janet Knott</title>
  <link rel="stylesheet" href="/style.css{style_version}" />
</head>
<body>
  <div class="container">
    <header>
      <h1>Janet Knott</h1>
      <p>Photographer. Photo essays from New England's art scene.</p>
      <nav class="site-nav">
        <a href="/home.html">Home</a>
        <a href="/tracker.html" aria-current="page">Globe Sightings</a>
        <a href="https://artspacerodeo.com" target="_blank" rel="noopener noreferrer">Art Space Rodeo &#8599;</a>
      </nav>
    </header>
    <main>
      <section class="tracker">
        <h2 class="section-title">Globe Sightings</h2>
        <p class="tracker-intro">Janet shot thousands of frames for the Boston Globe. Every
        so often the paper reaches back into the archive and one of them runs again, decades
        later, her byline along with it. This page watches for those reappearances.</p>
{body}      </section>
    </main>
    <footer>
      <p>Detection is automated and best-effort, last checked {generated_on}.
      Photo essays written for <a href="https://artspacerodeo.com" target="_blank" rel="noopener noreferrer">Art Space Rodeo</a>.</p>
    </footer>
  </div>
</body>
</html>
"""


def _item_html(s):
    title = escape(s.get("title") or "Untitled")
    # Link to the resolved Globe article when we have it, else the feed link.
    link = escape(s.get("article_url") or s.get("url") or "#", quote=True)
    pub = s.get("published") or s.get("discovered") or ""
    pub_label = pub
    if pub:
        try:
            pub_label = datetime.fromisoformat(pub).strftime("%B %-d, %Y")
        except ValueError:
            pub_label = pub

    shoot = s.get("shoot_year")
    ry = reuse_year(s)
    badge = ""
    if shoot and ry.isdigit() and int(ry) - int(shoot) > 0:
        gap = int(ry) - int(shoot)
        badge = (
            '\n          <p class="gap-badge">Shot {shoot} '
            '<span class="gap-dot">&middot;</span> resurfaced {ry} '
            '<span class="gap-dot">&middot;</span> '
            '<strong>{gap} year{plural} later</strong></p>'
        ).format(shoot=shoot, ry=ry, gap=gap, plural="s" if gap != 1 else "")

    # Attributed preview thumbnail (the Globe's own social-share image), when
    # we managed to fetch one. referrerpolicy keeps the hotlink low-profile;
    # if the CDN ever blocks it the figure simply collapses (alt text remains).
    figure = ""
    if s.get("image"):
        img_src = escape(s["image"], quote=True)
        figure = (
            f'\n          <a class="tracker-photo-link" href="{link}" target="_blank" rel="noopener noreferrer">'
            f'<img class="tracker-photo" src="{img_src}" alt="{title}" loading="lazy" referrerpolicy="no-referrer" /></a>'
            '\n          <p class="tracker-credit">Preview via The Boston Globe</p>'
        )

    return (
        '        <li class="tracker-item">'
        f'{figure}\n'
        f'          <a class="tracker-title" href="{link}" target="_blank" rel="noopener noreferrer">{title}</a>\n'
        f'          <time>{escape(pub_label)}</time>{badge}\n'
        "        </li>"
    )


def main():
    today_date = date.today()
    today = today_date.isoformat()
    since_year = today_date.year - RECENT_WINDOW_YEARS
    generated_on = datetime.now(timezone.utc).strftime("%B %-d, %Y")

    existing = load_sightings()
    found = []
    try:
        found = list(parse_items(fetch_feed(), since_year))
        print(f"Feed returned {len(found)} recent Globe item(s) crediting Janet Knott.")
    except Exception as exc:  # network/parse failure: still re-render from cache
        print(f"Feed fetch/parse failed ({exc!r}); rendering from existing data.")

    sightings, added = merge(existing, found, today)

    # Best-effort enrichment: resolve real Globe URLs, preview images, and the
    # original photo year for any not-yet-enriched sighting. Backfills existing
    # entries and retries ones whose article fetch previously failed.
    for s in sightings:
        if not s.get("enriched"):
            enrich(s)

    style_version = ""
    if STYLE_PATH.exists():
        digest = hashlib.md5(STYLE_PATH.read_bytes()).hexdigest()[:8]
        style_version = f"?v={digest}"

    SIGHTINGS_PATH.write_text(json.dumps(sightings, indent=2) + "\n")
    OUTPUT_PATH.write_text(render(sightings, generated_on, style_version))

    print(f"Added {len(added)} new sighting(s); {len(sightings)} total.")
    for s in added:
        print(f"  + {s['title']}")


if __name__ == "__main__":
    main()
