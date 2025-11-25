import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


"""
Simple Facebook Group watcher using the Graph API.

What it does
------------
- Periodically checks your group feed via the Facebook Graph API.
- Detects new posts (based on post ID) that contain shared links/photos.
- For each new post:
  - Saves the post metadata (text, permalink, original link if present).
  - Downloads all attached images.
- Keeps a local JSON file with "already processed" post IDs so you do not
  duplicate work.

What you must set up on Facebook
--------------------------------
1) Create a Facebook App (https://developers.facebook.com/apps)
2) Get a User Access Token with permissions:
   - groups_access_member_info (and any others Facebook requires)
3) Note your GROUP_ID (the numeric ID, not the name)
   - You can find it on https://lookup-id.com/ or similar tools
4) (Optional, but recommended) Convert your token to a long‑lived token
   so it does not expire quickly.

Limitations
-----------
- This script uses polling (it asks the API every N seconds).
- For true "instant" behavior, you would use Webhooks with a public HTTPS
  server, but polling is simpler to run on your own machine.
"""


GRAPH_API_BASE = "https://graph.facebook.com/v19.0"


@dataclass
class Config:
    group_id: str
    access_token: str
    download_dir: Path
    state_file: Path
    poll_interval_sec: int = 60  # how often to check for new posts
    page_size: int = 25          # how many posts per request


def load_config_from_env() -> Config:
    """
    Read configuration from environment variables.

    Required:
      FB_GROUP_ID      - your group numeric ID
      FB_ACCESS_TOKEN  - a valid user access token with group access

    Optional:
      FB_DOWNLOAD_DIR      - where to store data (default: ./fb_group_data)
      FB_POLL_INTERVAL_SEC - seconds between polls (default: 60)
      FB_PAGE_SIZE         - posts per API call (default: 25)
    """
    group_id = os.environ.get("FB_GROUP_ID", "").strip()
    access_token = os.environ.get("FB_ACCESS_TOKEN", "").strip()

    if not group_id or not access_token:
        raise RuntimeError(
            "You must set FB_GROUP_ID and FB_ACCESS_TOKEN environment variables.\n"
            "Example (Linux/macOS):\n"
            "  export FB_GROUP_ID=1234567890\n"
            "  export FB_ACCESS_TOKEN='EAAB...'\n\n"
            "Example (Windows CMD):\n"
            "  set FB_GROUP_ID=1234567890\n"
            "  set FB_ACCESS_TOKEN=EAAB...\n"
        )

    download_dir_str = os.environ.get("FB_DOWNLOAD_DIR", "").strip() or "fb_group_data"
    poll_interval_str = os.environ.get("FB_POLL_INTERVAL_SEC", "").strip() or "60"
    page_size_str = os.environ.get("FB_PAGE_SIZE", "").strip() or "25"

    try:
        poll_interval = max(10, int(poll_interval_str))
    except ValueError:
        poll_interval = 60

    try:
        page_size = max(1, min(100, int(page_size_str)))
    except ValueError:
        page_size = 25

    download_dir = Path(download_dir_str).resolve()
    state_file = download_dir / "seen_posts.json"

    return Config(
        group_id=group_id,
        access_token=access_token,
        download_dir=download_dir,
        state_file=state_file,
        poll_interval_sec=poll_interval,
        page_size=page_size,
    )


def load_seen_posts(state_file: Path) -> List[str]:
    if not state_file.is_file():
        return []
    try:
        with state_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data]
        return []
    except Exception:
        return []


def save_seen_posts(state_file: Path, post_ids: List[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.open("w", encoding="utf-8") as f:
        json.dump(sorted(set(post_ids)), f, ensure_ascii=False, indent=2)


def call_graph_api(
    endpoint: str, access_token: str, params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    url = f"{GRAPH_API_BASE}/{endpoint.lstrip('/')}"
    params = dict(params or {})
    params["access_token"] = access_token

    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Graph API error {resp.status_code}: {resp.text[:500]}"
        )
    return resp.json()


def get_group_feed(config: Config) -> List[Dict[str, Any]]:
    """
    Fetch the latest posts from the group feed.

    We request:
      - id
      - created_time
      - message (text)
      - story (some shared‑post text)
      - permalink_url (link to the post in the group)
      - attachments: media info, subattachments (for albums / multi‑images)
    """
    fields = (
        "id,created_time,message,story,permalink_url,"
        "attachments{media_type,media,url,subattachments}"
    )

    data = call_graph_api(
        f"{config.group_id}/feed",
        config.access_token,
        params={"fields": fields, "limit": config.page_size},
    )
    posts = data.get("data", [])
    return posts


def extract_images_from_attachments(attachments: Dict[str, Any]) -> List[str]:
    """
    Get every image URL from attachments/subattachments.
    """
    urls: List[str] = []
    if not attachments:
        return urls

    data = attachments.get("data") or []
    for att in data:
        media_type = (att.get("media_type") or "").lower()
        media = att.get("media") or {}

        if media_type in {"photo", "share", "album"} or media:
            # main image
            # Depending on API version, image URL can be in:
            # - media.image.src
            # - media.source
            img = media.get("image") or {}
            if "src" in img:
                urls.append(str(img["src"]))
            elif "source" in media:
                urls.append(str(media["source"]))

        # subattachments (album / multiple photos)
        sub = att.get("subattachments") or {}
        for sub_att in sub.get("data", []):
            sub_media = sub_att.get("media") or {}
            sub_img = sub_media.get("image") or {}
            if "src" in sub_img:
                urls.append(str(sub_img["src"]))
            elif "source" in sub_media:
                urls.append(str(sub_media["source"]))

    # remove duplicates
    return list(dict.fromkeys(urls))


def extract_original_link(attachments: Dict[str, Any]) -> Optional[str]:
    """
    Try to find the original link of a shared post.
    Often it is in attachments.data[0].url
    """
    if not attachments:
        return None
    data = attachments.get("data") or []
    for att in data:
        url = att.get("url")
        if url:
            return str(url)
    return None


def download_image(url: str, dest_dir: Path, prefix: str) -> Optional[Path]:
    """
    Download a single image, return local path or None on failure.
    """
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"[WARN] Failed to download image {url}: HTTP {resp.status_code}")
            return None

        # Try to guess extension from URL/content
        ext = ".jpg"
        for candidate in (".jpg", ".jpeg", ".png", ".webp"):
            if candidate in url.lower():
                ext = candidate
                break

        filename = dest_dir / f"{prefix}{ext}"
        with filename.open("wb") as f:
            f.write(resp.content)
        return filename
    except Exception as e:
        print(f"[WARN] Exception downloading {url}: {e}")
        return None


def process_post(post: Dict[str, Any], config: Config) -> None:
    """
    For a single post:
      - build folder: download_dir/post_{id}
      - save metadata.json
      - download all images
    """
    post_id = str(post.get("id", ""))
    if not post_id:
        return

    created_time = post.get("created_time", "")
    message = post.get("message") or ""
    story = post.get("story") or ""
    permalink = post.get("permalink_url") or ""
    attachments = post.get("attachments") or {}

    image_urls = extract_images_from_attachments(attachments)
    original_link = extract_original_link(attachments)

    post_dir = config.download_dir / f"post_{post_id.replace('/', '_')}"
    post_dir.mkdir(parents=True, exist_ok=True)

    # save metadata
    meta = {
        "id": post_id,
        "created_time": created_time,
        "message": message,
        "story": story,
        "permalink_url": permalink,
        "original_link": original_link,
        "image_urls": image_urls,
    }
    with (post_dir / "metadata.json").open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # save a simple text file with description and link (for easy copy/paste)
    description_lines = []
    if message:
        description_lines.append(message)
    elif story:
        description_lines.append(story)
    description_lines.append("")
    description_lines.append(f"Group post: {permalink}")
    if original_link:
        description_lines.append(f"Original link: {original_link}")

    with (post_dir / "description.txt").open("w", encoding="utf-8") as f:
        f.write("\n".join(description_lines))

    # download images
    if image_urls:
        print(f"[INFO] Post {post_id}: downloading {len(image_urls)} image(s)")
    else:
        print(f"[INFO] Post {post_id}: no images detected")

    for idx, url in enumerate(image_urls, start=1):
        local = download_image(url, post_dir, prefix=f"img_{idx}")
        if local:
            print(f"  -> {local}")


def run_once(config: Config) -> None:
    """
    Fetch the latest posts from the group and process only the new ones.
    """
    print("[INFO] Fetching group feed...")
    posts = get_group_feed(config)
    if not posts:
        print("[INFO] No posts returned from API.")
        return

    seen = load_seen_posts(config.state_file)
    seen_set = set(seen)

    new_ids: List[str] = []
    for post in posts:
        post_id = str(post.get("id", ""))
        if not post_id or post_id in seen_set:
            continue

        print(f"[INFO] New post detected: {post_id}")
        process_post(post, config)
        seen_set.add(post_id)
        new_ids.append(post_id)

    if new_ids:
        print(f"[INFO] Processed {len(new_ids)} new post(s).")
        save_seen_posts(config.state_file, list(seen_set))
    else:
        print("[INFO] No new posts to process.")


def run_polling_loop(config: Config) -> None:
    """
    Run forever: poll group feed every poll_interval_sec seconds.
    """
    print("===============================================")
    print(" Facebook Group watcher (Graph API, polling)  ")
    print("===============================================")
    print(f"Group ID:       {config.group_id}")
    print(f"Download dir:   {config.download_dir}")
    print(f"State file:     {config.state_file}")
    print(f"Poll interval:  {config.poll_interval_sec} seconds")
    print("")
    print("Press Ctrl+C to stop.")
    print("")

    config.download_dir.mkdir(parents=True, exist_ok=True)

    try:
        while True:
            try:
                run_once(config)
            except Exception as e:
                print(f"[ERROR] {e}")
            time.sleep(config.poll_interval_sec)
    except KeyboardInterrupt:
        print("\n[INFO] Stopped by user.")


def main() -> None:
    config = load_config_from_env()
    run_polling_loop(config)


if __name__ == "__main__":
    main()