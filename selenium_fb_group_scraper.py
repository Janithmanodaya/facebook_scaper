import csv
import sys
import time
from pathlib import Path

from typing import List, Dict

import random

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
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
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


def extract_posts_from_dom(driver: webdriver.Chrome, group_id_or_slug: str) -> List[Dict[str, str]]:
    """
    Extract posts from the live DOM using Selenium.

    Strategy:
    - Find post containers as elements with role="article" inside role="feed".
    - Inside each article, look for a link that contains "/groups/.../posts/...".
    - Collect:
      - post_url
      - post_text (visible text of the article)
      - image_urls (all <img> src values within the article)
    """
    posts: List[Dict[str, str]] = []
    try:
        articles = driver.find_elements(
            By.XPATH, "//div[@role='feed']//div[@role='article']"
        )
    except Exception as e:
        print(f"[DEBUG] Failed to locate post containers: {e}")
        return posts

    for art in articles:
        try:
            link_el = art.find_element(
                By.XPATH,
                ".//a[contains(@href, '/groups/') and contains(@href, '/posts/')]",
            )
            href = link_el.get_attribute("href") or ""
            if not href:
                continue

            text = art.text or ""

            # Collect image URLs within this article
            image_urls: List[str] = []
            try:
                img_elements = art.find_elements(By.XPATH, ".//img")
                for img in img_elements:
                    src = img.get_attribute("src") or ""
                    if src and src not in image_urls:
                        image_urls.append(src)
            except Exception:
                pass

            posts.append(
                {
                    "post_url": href,
                    "post_text": text[:4000],
                    "image_urls": image_urls,
                }
            )
        except Exception:
            # If we can't find a suitable link inside this article, skip it
            continue

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
    # Try to look more like a regular browser
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    # Uncomment the next line to try headless mode (not recommended for FB)
    # chrome_options.add_argument("--headless=new")

    driver = webdriver.Chrome(options=chrome_options)

    try:
        if cookies:
            attach_cookies(driver, cookies)
            driver.get(group_url)
            print("[INFO] Browser opened with your cookies applied.")
            print("      If you still see a login page, log in and open the group page manually,")
            input("      then press ENTER here to start scraping...")
        else:
            print("[INFO] No cookies provided. A browser window will open.")
            print("      Log in to Facebook in that window, navigate to the group page,")
            input("      then press ENTER here to start scraping...")
            driver.get("https://www.facebook.com/")
            # User is expected to log in and open the group page manually

        time.sleep(5)

        print("[INFO] Scrolling and collecting posts via Selenium...")
        collected: List[Dict[str, str]] = []
        seen_urls = set()
        scroll_pause_base = 2.5
        last_height = driver.execute_script("return document.body.scrollHeight")
        actions = ActionChains(driver)

        for _ in range(25):
            posts = extract_posts_from_dom(driver, gid)

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

            # Human-like scroll: move mouse randomly, then scroll
            try:
                actions.move_by_offset(random.randint(-50, 50), random.randint(-30, 30)).perform()
            except Exception:
                pass

            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.END)

            # Randomized pause between scrolls
            pause = scroll_pause_base + random.uniform(0.5, 2.5)
            time.sleep(pause)

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


# ------------------ Simple Tkinter UI wrapper ------------------ #

try:
    import threading
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    tk = None  # UI will not be available


class SeleniumScraperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Facebook Group Selenium Scraper")
        self.geometry("700x260")
        self.minsize(650, 230)

        self.group_var = tk.StringVar()
        self.keyword_var = tk.StringVar()
        self.max_posts_var = tk.StringVar(value="50")
        self.cookies_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Idle")

        self._build_ui()

    def _build_ui(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text="Group URL / ID:").grid(row=0, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(frm, textvariable=self.group_var, width=60).grid(
            row=0, column=1, columnspan=3, sticky="we", pady=4
        )

        ttk.Label(frm, text="Keyword (optional):").grid(row=1, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(frm, textvariable=self.keyword_var, width=30).grid(
            row=1, column=1, sticky="we", pady=4
        )

        ttk.Label(frm, text="Max posts:").grid(row=1, column=2, sticky="e", padx=5, pady=4)
        ttk.Entry(frm, textvariable=self.max_posts_var, width=10).grid(
            row=1, column=3, sticky="w", pady=4
        )

        ttk.Label(frm, text="Cookies file (cookies.txt):").grid(
            row=2, column=0, sticky="e", padx=5, pady=4
        )
        ttk.Entry(frm, textvariable=self.cookies_var, width=45).grid(
            row=2, column=1, sticky="we", pady=4
        )
        ttk.Button(frm, text="Browse...", command=self._on_browse).grid(
            row=2, column=2, sticky="w", padx=5, pady=4
        )

        ttk.Button(frm, text="Start Scrape", command=self._on_start).grid(
            row=3, column=2, sticky="e", padx=5, pady=10
        )
        ttk.Button(frm, text="Close", command=self.destroy).grid(
            row=3, column=3, sticky="w", padx=5, pady=10
        )

        status_lbl = ttk.Label(frm, textvariable=self.status_var)
        status_lbl.grid(row=4, column=0, columnspan=4, sticky="w", padx=5, pady=(5, 0))

        for i in range(4):
            frm.columnconfigure(i, weight=1)

    def _on_browse(self):
        path = filedialog.askopenfilename(
            title="Select cookies.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.cookies_var.set(path)

    def _run_scrape(self, group_input, keyword, max_posts, cookies_file):
        self.status_var.set("Running Selenium scraper... see console for logs.")
        self.update_idletasks()

        # Call the existing CLI-based main logic but bypass input()
        # We replicate main() behavior here using helper function.
        try:
            run_selenium_scrape(group_input, keyword, max_posts, cookies_file)
            self.status_var.set("Done. Check fb_group_posts_selenium.csv and console logs.")
        except Exception as e:
            self.status_var.set("Error.")
            messagebox.showerror("Error", str(e))

    def _on_start(self):
        group_input = self.group_var.get().strip()
        keyword = self.keyword_var.get().strip()
        max_posts_str = self.max_posts_var.get().strip() or "50"
        cookies_file = self.cookies_var.get().strip() or ""

        if not group_input:
            messagebox.showerror("Error", "Please enter a group URL or ID.")
            return

        try:
            max_posts = int(max_posts_str)
            if max_posts <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Max posts must be a positive integer.")
            return

        t = threading.Thread(
            target=self._run_scrape,
            args=(group_input, keyword, max_posts, cookies_file),
            daemon=True,
        )
        t.start()


def run_selenium_scrape(group_input: str, keyword: str, max_posts: int, cookies_path_str: str):
    """
    Non-interactive wrapper around main() logic so we can call it from the GUI.
    """
    keyword = (keyword or "").strip().lower()
    cookies = []
    if cookies_path_str:
        cookies_path = Path(cookies_path_str)
        if not cookies_path.is_file():
            raise FileNotFoundError(f"Cookies file not found: {cookies_path}")
        cookies = load_netscape_cookies(cookies_path)
        print(f"[INFO] Loaded {len(cookies)} cookies from {cookies_path}")
    else:
        cookies_path = None

    group_url = normalize_group_url(group_input)
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

    driver = webdriver.Chrome(options=chrome_options)

    try:
        if cookies:
            attach_cookies(driver, cookies)
            driver.get(group_url)
            print("[INFO] Browser opened with your cookies applied.")
            print("      If you still see a login page, log in and open the group page manually,")
            input("      then press ENTER here in the console to start scraping...")
        else:
            print("[INFO] No cookies provided. A browser window will open.")
            print("      Log in to Facebook in that window, navigate to the group page,")
            input("      then press ENTER here in the console to start scraping...")
            driver.get("https://www.facebook.com/")
            # User is expected to log in and open the group page manually

        time.sleep(5)

        print("[INFO] Scrolling and collecting posts via Selenium...")
        collected: List[Dict[str, str]] = []
        seen_urls = set()
        scroll_pause_base = 2.5
        last_height = driver.execute_script("return document.body.scrollHeight")
        actions = ActionChains(driver)

        for _ in range(25):
            posts = extract_posts_from_dom(driver, gid)

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

            # Human-like scroll: move mouse randomly, then scroll
            try:
                actions.move_by_offset(random.randint(-50, 50), random.randint(-30, 30)).perform()
            except Exception:
                pass

            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.END)

            # Randomized pause between scrolls
            pause = scroll_pause_base + random.uniform(0.5, 2.5)
            time.sleep(pause)

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
    if tk is not None:
        app = SeleniumScraperApp()
        app.mainloop()
    else:
        main()