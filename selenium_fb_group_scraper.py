import csv
import os
import random
import re
import sys
import time
import html as html_lib
from pathlib import Path
from typing import Dict, List, Optional

try:
    import requests
except ImportError:
    print("The 'requests' package is not installed.")
    print("Install it with:")
    print("    python -m pip install requests")
    sys.exit(1)

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
except ImportError:
    print("The 'selenium' package is not installed.")
    print("Install it with:")
    print("    python -m pip install selenium")
    sys.exit(1)

# Facebook image URL pattern (keep query string, do NOT cut at .jpg/.png)
FB_IMAGE_URL_REGEX = re.compile(
    r"https?://[^\"'\s]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\"'\s]*)?",
    re.IGNORECASE,
)


def load_netscape_cookies(cookies_path: Path) -> List[Dict[str, str]]:
    """
    Load cookies from a Netscape cookies.txt file.

    Lines look like:
    .facebook.com  TRUE    /   TRUE    1893456000  c_user  123456789
    """
    cookies: List[Dict[str, str]] = []
    with cookies_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 7:
                # Some exporters use spaces instead of tabs
                parts = line.split()
                if len(parts) != 7:
                    continue

            domain, flag, path, secure, expiration, name, value = parts
            cookies.append(
                {
                    "domain": domain,
                    "path": path,
                    "name": name,
                    "value": value,
                    "secure": secure.upper() == "TRUE",
                }
            )
    return cookies


def attach_cookies(driver: webdriver.Chrome, cookies: List[Dict[str, str]]) -> None:
    """
    Attach cookies to a Facebook session.
    """
    driver.get("https://www.facebook.com/")
    time.sleep(3 + random.uniform(0.5, 2.0))
    for c in cookies:
        cookie = {
            "domain": c["domain"],
            "path": c["path"],
            "name": c["name"],
            "value": c["value"],
            "secure": c["secure"],
        }
        try:
            driver.add_cookie(cookie)
        except Exception as e:
            print(f"[DEBUG] Failed to add cookie {c['name']}: {e}")


def build_cookie_header(cookies: List[Dict[str, str]]) -> str:
    """
    Build a Cookie header string suitable for requests from Netscape cookies.
    """
    if not cookies:
        return ""
    parts = []
    for c in cookies:
        if not c.get("name"):
            continue
        parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def setup_driver(headless: bool = True) -> webdriver.Chrome:
    """
    Create a Chrome WebDriver, headless by default.
    """
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=chrome_options)
    return driver


def extract_single_post(
    driver: webdriver.Chrome, post_url: str
) -> Optional[Dict[str, object]]:
    """
    Open a single Facebook post URL and extract:
    - post_url
    - post_text
    - image_urls (list)
    """
    print(f"[INFO] Opening post URL: {post_url}")
    driver.get(post_url)

    # Wait a bit for content to load
    time.sleep(8 + random.uniform(1.0, 3.0))

    text = ""
    html = ""

    # Try to find the specific story_message container first
    try:
        story_divs = driver.find_elements(
            By.XPATH, "//div[@data-ad-rendering-role='story_message']"
        )
        if story_divs:
            target = story_divs[0]
        else:
            # Fallback: first article on the page
            target = driver.find_element(By.XPATH, "//div[@role='article']")
    except Exception:
        try:
            target = driver.find_element(By.TAG_NAME, "body")
        except Exception:
            print("[ERROR] Could not locate post content on the page.")
            return None

    try:
        text = target.text or ""
    except Exception:
        text = ""

    try:
        html = target.get_attribute("innerHTML") or ""
    except Exception:
        html = ""

    # Fallback: if Selenium text is empty, derive rough plain text from HTML
    if not text and html:
        rough = re.sub(r"<[^>]+>", " ", html)
        rough = " ".join(rough.split())
        text = rough

    image_urls: List[str] = []

    # 1) Direct <img> tags in the target container
    try:
        img_elements = target.find_elements(By.XPATH, ".//img")
        for img in img_elements:
            src = img.get_attribute("src") or ""
            if not src or src.startswith("data:"):
                continue
            if src not in image_urls:
                image_urls.append(src)
    except Exception:
        pass

    # 2) Fallback: scan the whole page HTML for image URLs (fbcdn, scontent, etc.)
    try:
        page_html = driver.page_source or ""
    except Exception:
        page_html = html

    if page_html:
        for match in FB_IMAGE_URL_REGEX.findall(page_html):
            clean_url = html_lib.unescape(match)
            if clean_url not in image_urls:
                image_urls.append(clean_url)

    print(f"[INFO] Extracted post text length: {len(text)}")
    print(f"[INFO] Found {len(image_urls)} image URL(s) in the post.")

    return {
        "post_url": post_url,
        "post_text": text[:4000],
        "image_urls": image_urls,
    }


def download_images_for_post(
    post: Dict[str, object],
    cookies: Optional[List[Dict[str, str]]] = None,
) -> None:
    """
    Download images for a single post and attach 'image_paths' (semicolon-separated)
    to the post dict. Images are saved under fb_images/post_img{j}.jpg
    """
    image_urls: List[str] = post.get("image_urls") or []  # type: ignore[assignment]
    if not image_urls:
        print("[INFO] No image URLs to download for this post.")
        post["image_paths"] = ""
        return

    # Always save images next to this script, in a fixed "fb_images" folder
    script_dir = Path(__file__).resolve().parent
    img_dir = script_dir / "fb_images"
    img_dir.mkdir(exist_ok=True)

    headers_base: Dict[str, str] = {}
    if cookies:
        cookie_header = build_cookie_header(cookies)
        if cookie_header:
            headers_base["Cookie"] = cookie_header

    # Try to mimic a real browser
    headers_base.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    headers_base.setdefault(
        "Accept",
        "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    )
    headers_base.setdefault("Accept-Language", "en-US,en;q=0.9")
    headers_base.setdefault("Connection", "keep-alive")

    post_url = post.get("post_url", "https://www.facebook.com/")
    headers = dict(headers_base)
    headers["Referer"] = str(post_url)

    local_paths: List[str] = []
    for j, url in enumerate(image_urls, start=1):
        if url.startswith("data:"):
            print("[DEBUG] Skipping inline image data URI")
            continue
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code != 200:
                print(
                    f"[DEBUG] Failed to download image {url}: "
                    f"HTTP {resp.status_code}"
                )
                continue
            ext = ".jpg"
            filename = img_dir / f"post_img{j}{ext}"
            with filename.open("wb") as f:
                f.write(resp.content)
            local_paths.append(str(filename.resolve()))
        except Exception as e:
            print(f"[DEBUG] Exception downloading image {url}: {e}")
            continue

    post["image_paths"] = ";".join(local_paths)


def save_post_to_csv(post: Dict[str, object], out_path: Path) -> None:
    """
    Save a single post to CSV: post_url, post_text, image_paths.
    """
    fieldnames = ["post_url", "post_text", "image_paths"]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "post_url": post.get("post_url", ""),
                "post_text": post.get("post_text", ""),
                "image_paths": post.get("image_paths", ""),
            }
        )
    print(f"[INFO] Saved CSV to {out_path}")


def run_single_post_scrape(
    post_url: str,
    cookies_path_str: str = "",
    headless: bool = True,
) -> Optional[Dict[str, object]]:
    """
    Entry point: open post URL, extract description and image URLs, download images,
    and save CSV next to this script.
    """
    post_url = post_url.strip()
    if not post_url:
        raise ValueError("Post URL is required.")

    cookies: List[Dict[str, str]] = []
    if cookies_path_str:
        cookies_path = Path(cookies_path_str)
        if not cookies_path.is_file():
            raise FileNotFoundError(f"Cookies file not found: {cookies_path}")
        cookies = load_netscape_cookies(cookies_path)
        print(f"[INFO] Loaded {len(cookies)} cookies from {cookies_path}")

    driver = setup_driver(headless=headless)

    try:
        if cookies:
            attach_cookies(driver, cookies)
            # Re-open the post after cookies applied
            driver.get(post_url)
            time.sleep(5 + random.uniform(1.0, 2.0))

        post = extract_single_post(driver, post_url)
        if not post:
            print("[INFO] No post data extracted.")
            return None

        download_images_for_post(post, cookies=cookies or None)

        script_dir = Path(__file__).resolve().parent
        out_path = script_dir / "fb_single_post.csv"
        save_post_to_csv(post, out_path)

        images_dir = script_dir / "fb_images"
        print(f"[INFO] Images (if any) are in: {images_dir}")
        print(f"[INFO] Post URL: {post['post_url']}")
        print(f"[INFO] Description (first 300 chars): {str(post['post_text'])[:300]}")
        return post
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def main():
    print("=== Facebook Single Post Scraper (headless) ===")
    post_url = input("Enter exact Facebook post URL: ").strip()
    cookies_path_str = input(
        "Path to cookies.txt (optional, press Enter to skip): "
    ).strip()

    if not post_url:
        print("Post URL is required.")
        return

    try:
        post = run_single_post_scrape(
            post_url=post_url,
            cookies_path_str=cookies_path_str,
            headless=True,
        )
        if not post:
            print("No data extracted.")
            return
    except Exception as e:
        print(f"Error: {e}")
        return

    print("Done.")


if __name__ == "__main__":
    main()