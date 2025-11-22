import csv
import sys
import time
from pathlib import Path

from typing import List, Dict

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
except ImportError:
    print("The 'selenium' package is not installed.")
    print("Install it with:")
    print("    python -m pip install selenium")
    sys.exit(1)


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
    # We must be on the base domain before adding cookies
    driver.get("https://www.facebook.com/")
    time.sleep(3)
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


def normalize_group_url(raw: str) -> str:
    raw = raw.strip()
    if "/groups/" in raw:
        # Ensure we hit the posts tab
        if "?" in raw:
            raw = raw.split("?", 1)[0]
        if not raw.endswith("/"):
            raw += "/"
        if "/posts/" not in raw:
            raw += "posts"
    return raw


def extract_posts_from_page_source(html: str, group_id_or_slug: str) -> List[Dict[str, str]]:
    """
    Very heuristic extraction using simple string search.
    For robustness, we avoid depending on fragile CSS classes.

    We search for '/groups/<id>/posts/' links and take some surrounding text.
    """
    posts: List[Dict[str, str]] = []
    marker = f"/groups/{group_id_or_slug}/posts/"
    idx = 0
    while True:
        idx = html.find(marker, idx)
        if idx == -1:
            break

        # Extract URL
        start = html.rfind('"', 0, idx)
        end = html.find('"', idx)
        if start == -1 or end == -1:
            idx += len(marker)
            continue
        href = html[start + 1 : end]
        if href.startswith("/"):
            href = "https://www.facebook.com" + href

        # Extract some text around the link as "post text"
        context_start = max(0, start - 800)
        context_end = min(len(html), end + 800)
        context = html[context_start:context_end]

        # Very rough text cleanup
        text = (
            context.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&amp;", "&")
        )

        posts.append(
            {
                "post_url": href,
                "post_text": text[:1000],  # limit size
            }
        )

        idx = end

    return posts


def main():
    print("=== Selenium Facebook Group Scraper (experimental) ===")

    group_input = input("Enter Facebook group URL or ID: ").strip()
    keyword = input("Enter keyword to filter by (leave empty for all): ").strip().lower()
    max_posts_str = input("Max posts to save (default 50): ").strip() or "50"
    cookies_path_str = input("Path to cookies.txt (recommended, enter to skip): ").strip()

    try:
        max_posts = int(max_posts_str)
        if max_posts <= 0:
            raise ValueError
    except ValueError:
        print("Invalid max posts, using 50.")
        max_posts = 50

    cookies_path = None
    cookies = []
    if cookies_path_str:
        cookies_path = Path(cookies_path_str)
        if not cookies_path.is_file():
            print(f"Cookies file not found: {cookies_path}")
            return
        cookies = load_netscape_cookies(cookies_path)
        print(f"[INFO] Loaded {len(cookies)} cookies from {cookies_path}")

    group_url = normalize_group_url(group_input)
    # Extract numeric ID or slug for link detection
    gid = group_input
    if "facebook.com" in group_input and "/groups/" in group_input:
        tail = group_input.split("/groups/", 1)[1]
        for sep in ("?", "#", "/"):
            if sep in tail:
                tail = tail.split(sep, 1)[0]
        gid = tail

    print(f"[INFO] Normalized group URL: {group_url}")
    print(f"[INFO] Using group identifier for parsing: {gid}")

    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    # Uncomment the next line to try headless mode (not recommended for FB)
    # chrome_options.add_argument("--headless=new")

    driver = webdriver.Chrome(options=chrome_options)

    try:
        if cookies:
            attach_cookies(driver, cookies)
            # After attaching cookies, go directly to group
            driver.get(group_url)
        else:
            # No cookies: you may need to log in manually
            print("[INFO] No cookies provided. A browser window will open.")
            print("      Log in to Facebook in that window, then press ENTER here.")
            driver.get("https://www.facebook.com/")
            input("After you are logged in in the browser window, press ENTER here...")
            driver.get(group_url)

        time.sleep(5)

        print("[INFO] Scrolling and collecting posts...")
        collected: List[Dict[str, str]] = []
        seen_urls = set()

        scroll_pause = 3
        last_height = driver.execute_script("return document.body.scrollHeight")

        for i in range(20):  # 20 scroll iterations
            html = driver.page_source
            posts = extract_posts_from_page_source(html, gid)

            for p in posts:
                url = p["post_url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                text_lower = p["post_text"].lower()
                if keyword and keyword not in text_lower:
                    continue

                collected.append(p)
                print(f"[DEBUG] Collected post #{len(collected)}: {url}")

                if len(collected) >= max_posts:
                    break

            if len(collected) >= max_posts:
                break

            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.END)
            time.sleep(scroll_pause)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                print("[INFO] Reached bottom of page or no new content.")
                break
            last_height = new_height

        print(f"[INFO] Finished. Collected {len(collected)} post(s) matching filter.")

        if not collected:
            return

        out_path = Path("fb_group_posts_selenium.csv")
        with out_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["post_url", "post_text"])
            writer.writeheader()
            for row in collected:
                writer.writerow(row)

        print(f"[INFO] Saved results to {out_path.resolve()}")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()