from __future__ import annotations

import argparse
import email
from email import policy
import mimetypes
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urldefrag, urlsplit, urlunsplit

from bs4 import BeautifulSoup


TWITTER_MEDIA_RE = re.compile(r"^https?://pbs\.twimg\.com/media/", re.I)


def normalize_url(url: str) -> str:
    """Normalize URL for comparison (drop fragment and keep query)."""
    url, _frag = urldefrag(url.strip())
    return url


def url_no_query(url: str) -> str:
    """Normalize URL further by removing the query string."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def random_digits_like(value: str) -> str:
    """Generate a random numeric string with the same length as *value*."""
    n = len(value)
    if n <= 0:
        return "0"
    return str(secrets.randbelow(10**n)).zfill(n)


def safe_name(value: str, default: str = "unknown") -> str:
    """Make a filename/folder-name safe on Windows, macOS, and Linux."""
    value = (value or "").strip()
    if not value:
        value = default
    value = value.replace("/", "_").replace("\\", "_")
    value = re.sub(r'[<>:"|?*\x00-\x1F]', "_", value)
    value = value.rstrip(" .")
    return value or default


def parse_mhtml(path: Path) -> Tuple[str, Dict[str, bytes], Dict[str, str]]:
    """
    Return:
      html_text, image_bytes_by_url, content_type_by_url
    """
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)

    html_text: Optional[str] = None
    image_bytes_by_url: Dict[str, bytes] = {}
    content_type_by_url: Dict[str, str] = {}

    for part in msg.walk():
        ctype = part.get_content_type()

        if ctype == "text/html" and html_text is None:
            html_text = part.get_content()
            continue

        if not ctype.startswith("image/"):
            continue

        payload = part.get_content()
        if not isinstance(payload, (bytes, bytearray)):
            # Defensive fallback; should not normally happen for images.
            continue

        loc = part.get("Content-Location")
        if loc:
            loc = normalize_url(loc)
            image_bytes_by_url.setdefault(loc, bytes(payload))
            content_type_by_url.setdefault(loc, ctype)

            # Add queryless variant too, for better matching.
            loc2 = url_no_query(loc)
            image_bytes_by_url.setdefault(loc2, bytes(payload))
            content_type_by_url.setdefault(loc2, ctype)

        cid = part.get("Content-ID")
        if cid:
            cid = cid.strip("<>")
            image_bytes_by_url.setdefault(f"cid:{cid}", bytes(payload))
            content_type_by_url.setdefault(f"cid:{cid}", ctype)

    if html_text is None:
        raise RuntimeError("No HTML part found in MHTML file.")

    return html_text, image_bytes_by_url, content_type_by_url


def extract_tweets(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    return soup.select("div.tweet")


def format_utc_from_timestamp_ms(value: Optional[str], fallback: Optional[datetime] = None) -> str:
    if value:
        try:
            ms = int(value)
            dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")   # ← fixed colon
        except Exception:
            pass

    dt = fallback or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")            # ← fixed colon


def ext_from_url_or_type(url: str, content_type: Optional[str]) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if ext:
            return ".jpg" if ext == ".jpe" else ext
    path = urlsplit(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".bin"


def extract_images(mhtml_path: Path, output_dir: Path) -> None:
    html_text, image_map, ctype_map = parse_mhtml(mhtml_path)
    tweets = extract_tweets(html_text)

    output_dir.mkdir(parents=True, exist_ok=True)

    total_saved = 0
    total_missing = 0

    for tweet in tweets:
        author = tweet.select_one(".tweet-header-handle")
        author = author.get_text(strip=True).lstrip("@") if author else "unknown"
        author = safe_name(author)

        tweet_id = tweet.get("data-tweet-id") or ""
        if not tweet_id:
            # Generate a random numeric id with the same length as a typical snowflake id.
            tweet_id = random_digits_like("0000000000000000000")

        time_el = tweet.select_one(".tweet-time")
        dt = format_utc_from_timestamp_ms(time_el.get("data-timestamp") if time_el else None)

        media_imgs = tweet.select("img.tweet-media-element")
        if not media_imgs:
            continue

        author_dir = output_dir / author
        author_dir.mkdir(parents=True, exist_ok=True)

        for idx, img in enumerate(media_imgs, start=1):
            src = normalize_url(img.get("src", ""))
            if not src:
                continue

            payload = None
            ctype = None

            # Exact match first, then without query.
            for key in (src, url_no_query(src)):
                if key in image_map:
                    payload = image_map[key]
                    ctype = ctype_map.get(key)
                    break

            if payload is None and src.startswith("data:image/"):
                # Handle inline base64 data URIs if they appear inside tweet media.
                try:
                    header, b64data = src.split(",", 1)
                    if ";base64" in header:
                        import base64
                        payload = base64.b64decode(b64data)
                        ctype = header.split(":", 1)[1].split(";", 1)[0]
                except Exception:
                    payload = None

            if payload is None:
                total_missing += 1
                print(f"[WARN] Missing embedded image for {author} tweet {tweet_id} image #{idx}: {src}")
                continue

            ext = ext_from_url_or_type(src, ctype)
            filename = f"{author} [{dt}] {tweet_id}_{idx}{ext}"
            filename = safe_name(filename)

            out_path = author_dir / filename
            out_path.write_bytes(payload)
            total_saved += 1

    print(f"Saved: {total_saved}")
    print(f"Missing (referenced in HTML, not embedded in MHTML): {total_missing}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract tweet media images from an MHTML/MHT file into author folders."
    )
    parser.add_argument(
        "-f", "--file", dest="input_file", required=True,
        help="Path to .mhtml/.mht file"
    )
    parser.add_argument(
        "-o", "--output", dest="output_dir", default="extracted_images",
        help="Output folder (default: extracted_images)"
    )
    args = parser.parse_args()

    extract_images(Path(args.input_file), Path(args.output_dir))


if __name__ == "__main__":
    main()
