import csv
import os
import random
import re
import sys
import time
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional


# Global flag controlled by UI/CLI to filter posts by Sri Lankan phone numbers
SL_FILTER_ENABLED = False

# Rough pattern for Sri Lankan phone numbers:
# - +94XXXXXXXX or +94XXXXXXXXX (country code +94 and 8–9 digits)
# - 03XXXXXXXX or 07XXXXXXXX (local formats starting with 03 or 07 and 8 digits)
SL_PHONE_REGEX = re.compile(r"(?:\+94\d{8,9}|0(?:3|7)\d{7,8})")


def contains_sl_phone(text: str) -> bool:
    if not text:
        return False
    return SL_PHONE_REGEX.search(text) is not None


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
    from selenium.webdriver.common.action_chains import ActionChains
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


def extract_posts_from_dom(
    driver: webdriver.Chrome,
    group_id_or_slug: str,
) -> List[Dict[str, str]]:
    """
    Extract posts from the live DOM using Selenium.

    We try to be robust to Facebook layout changes:

    - First, find all elements with role="article" anywhere on the page.
    - For each article, try several strategies to locate a canonical post link:
      1. A link containing "/groups/<group_id_or_slug>/posts/" or "/permalink/"
      2. Any link containing "/groups/" and "/posts/"
      3. As a final fallback, any link containing "/posts/"
    - We then collect:
      - post_url
      - post_text (visible text of the article)
      - html (innerHTML of the article, used as a fallback for phone detection)
      - image_urls (all <img> src values within the article)
    """
    posts: List[Dict[str, str]] = []

    try:
        articles = driver.find_elements(By.XPATH, "//div[@role='article']")
    except Exception as e:
        print(f"[DEBUG] Failed to locate post containers: {e}")
        return posts

    print(f"[DEBUG] Found {len(articles)} candidate article elements on the page.")

    gid = (group_id_or_slug or "").strip()

    for idx, art in enumerate(articles, start=1):
        href = ""
        link_el = None

        # Strategy 1: explicit group id/slug in the URL
        if gid:
            try:
                xpath = (
                    ".//a[contains(@href, '/groups/"
                    + gid
                    + "/posts/') or contains(@href, '/groups/"
                    + gid
                    + "/permalink/')]"
                )
                link_el = art.find_element(By.XPATH, xpath)
            except Exception:
                link_el = None

        # Strategy 2: generic groups + posts pattern
        if link_el is None:
            try:
                link_el = art.find_element(
                    By.XPATH,
                    ".//a[contains(@href, '/groups/') and contains(@href, '/posts/')]",
                )
            except Exception:
                link_el = None

        # Strategy 3: any link with "/posts/"
        if link_el is None:
            try:
                link_el = art.find_element(By.XPATH, ".//a[contains(@href, '/posts/')]")
            except Exception:
                link_el = None

        if link_el is None:
            snippet = (art.text or "").replace("\n", " ")[:80]
            print(f"[DEBUG] Article #{idx}: no post link found. Snippet='{snippet}'")
            continue

        href = link_el.get_attribute("href") or ""
        if not href:
            continue

        text = art.text or ""
        html = ""
        try:
            html = art.get_attribute("innerHTML") or ""
        except Exception:
            html = ""

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
                "html": html,
                "image_urls": image_urls,
            }
        )

    print(f"[DEBUG] extract_posts_from_dom: returning {len(posts)} post(s).")
    return posts


def compute_dynamic_delay(iter_index: int, base: float = 2.5) -> float:
    """
    Compute a human-like delay between scrolls.

    - base: base seconds
    - random jitter: ±0.8s
    - small backoff as iter_index grows (scrolls get gradually slower)
    """
    jitter = random.uniform(-0.8, 0.8)
    backoff_steps = iter_index // 5  # 0,1,2,...
    backoff = backoff_steps * random.uniform(0.3, 0.6)
    delay = base + jitter + backoff
    return max(delay, 0.8)


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


def download_images_for_posts(
    posts: List[Dict[str, str]],
    cookies: Optional[List[Dict[str, str]]] = None,
) -> None:
    """
    Download images for each post and attach 'image_paths' (semicolon-separated)
    to each post dict. Images are saved under fb_images/post_{i}_img{j}.jpg
    """
    if not posts:
        return

    img_dir = Path("fb_images")
    img_dir.mkdir(exist_ok=True)

    headers = {}
    if cookies:
        cookie_header = build_cookie_header(cookies)
        if cookie_header:
            headers["Cookie"] = cookie_header
    # A minimal UA helps slightly
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )

    for i, post in enumerate(posts, start=1):
        image_urls = post.get("image_urls") or []
        local_paths: List[str] = []
        for j, url in enumerate(image_urls, start=1):
            try:
                resp = requests.get(url, headers=headers, timeout=20)
                if resp.status_code != 200:
                    print(f"[DEBUG] Failed to download image {url}: HTTP {resp.status_code}")
                    continue
                ext = ".jpg"
                filename = img_dir / f"post_{i}_img{j}{ext}"
                with filename.open("wb") as f:
                    f.write(resp.content)
                local_paths.append(str(filename.resolve()))
            except Exception as e:
                print(f"[DEBUG] Exception downloading image {url}: {e}")
                continue
        post["image_paths"] = ";".join(local_paths)


def save_posts_to_csv(posts: List[Dict[str, str]], out_path: Path) -> None:
    """
    Save posts to CSV with columns: post_url, post_text, image_paths.
    """
    if not posts:
        return

    fieldnames = ["post_url", "post_text", "image_paths"]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in posts:
            writer.writerow(
                {
                    "post_url": p.get("post_url", ""),
                    "post_text": p.get("post_text", ""),
                    "image_paths": p.get("image_paths", ""),
                }
            )


def _extract_group_id_or_slug(group_input: str) -> str:
    """
    Extract numeric ID or slug from a group URL or return the input as-is.
    """
    gid = group_input.strip()
    if "facebook.com" in gid and "/groups/" in gid:
        tail = gid.split("/groups/", 1)[1]
        for sep in ("?", "#", "/"):
            if sep in tail:
                tail = tail.split(sep, 1)[0]
        gid = tail
    return gid


def selenium_collect_posts(
    group_input: str,
    keyword: str,
    max_posts: int,
    cookies: Optional[List[Dict[str, str]]] = None,
    only_sl_phones: bool = False,
) -> List[Dict[str, str]]:
    """
    Core Selenium scraping routine (no GUI, no CSV). Returns a list of post dicts:
    - post_url
    - post_text
    - image_urls (list)
    """
    keyword = (keyword or "").strip().lower()
    group_url = normalize_group_url(group_input)
    gid = _extract_group_id_or_slug(group_input)

    print(f"[INFO] Normalized group URL: {group_url}")
    print(f"[INFO] Using group identifier for parsing: {gid}")

    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(options=chrome_options)

    try:
        if cookies:
            attach_cookies(driver, cookies)
            driver.get(group_url)
            print("[INFO] Browser opened with your cookies applied.")
        else:
            print("[INFO] No cookies provided. A browser window will open.")
            driver.get(group_url)
            # Facebook may redirect to login; user must log in and then open the group page.

        print(
            "[INFO] Please log in to Facebook in the opened browser (if not already), "
            "then navigate to the group page. The scraper will start automatically once "
            "it detects a group URL, or after a timeout."
        )

        # Wait (up to ~5 minutes) for the user to log in and open the group page.
        max_wait_seconds = 300
        start_wait = time.time()
        while time.time() - start_wait < max_wait_seconds:
            try:
                current_url = driver.current_url
            except Exception:
                current_url = ""
            if "/groups/" in (current_url or ""):
                break
            time.sleep(3)

        time.sleep(5)

        print("[INFO] Scrolling and collecting posts via Selenium...")
        collected: List[Dict[str, str]] = []
        seen_urls = set()
        last_height = driver.execute_script("return document.body.scrollHeight")
        actions = ActionChains(driver)

        for scroll_idx in range(25):
            posts = extract_posts_from_dom(driver, gid)

            for p in posts:
                url = p["post_url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                text_lower = p["post_text"].lower()
                html_lower = (p.get("html") or "").lower()

                # Sri Lankan phone filter first (if enabled)
                if only_sl_phones and not (contains_sl_phone(text_lower) or contains_sl_phone(html_lower)):
                    continue

                # Keyword filter (if provided)
                if keyword and (keyword not in text_lower and keyword not in html_lower):
                    continue

                collected.append(p)
                print(f"[DEBUG] Collected post #{len(collected)}: {url}")

                if len(collected) >= max_posts:
                    break

            if len(collected) >= max_posts:
                break

            # Human-like scroll: move mouse randomly, then scroll
            try:
                actions.move_by_offset(
                    random.randint(-50, 50), random.randint(-30, 30)
                ).perform()
            except Exception:
                pass

            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.END)

            # Dynamic delay between scrolls
            pause = compute_dynamic_delay(scroll_idx, base=2.5)
            time.sleep(pause)

            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                print("[INFO] Reached bottom of page or no new content.")
                break
            last_height = new_height

        print(f"[INFO] Finished. Collected {len(collected)} post(s) matching filter.")
        return collected

    finally:
        driver.quit()


def main():
    print("=== Selenium Facebook Group Scraper (experimental) ===")

    group_input = input("Enter Facebook group URL or ID: ").strip()
    keyword = input("Enter keyword to filter by (leave empty for all): ").strip()
    max_posts_str = input("Max posts to save (default 50): ").strip() or "50"
    cookies_path_str = input(
        "Path to cookies.txt (recommended, enter to skip): "
    ).strip()
    sl_only_str = input(
        "Only posts with Sri Lankan phone number? (y/N): "
    ).strip().lower()

    try:
        max_posts = int(max_posts_str)
        if max_posts <= 0:
            raise ValueError
    except ValueError:
        print("Invalid max posts, using 50.")
        max_posts = 50

    cookies_path = None
    cookies: List[Dict[str, str]] = []
    if cookies_path_str:
        cookies_path = Path(cookies_path_str)
        if not cookies_path.is_file():
            print(f"Cookies file not found: {cookies_path}")
            input("Press Enter to exit...")
            return
        cookies = load_netscape_cookies(cookies_path)
        print(f"[INFO] Loaded {len(cookies)} cookies from {cookies_path}")

    only_sl = sl_only_str in {"y", "yes"}

    collected = selenium_collect_posts(
        group_input=group_input,
        keyword=keyword,
        max_posts=max_posts,
        cookies=cookies or None,
        only_sl_phones=only_sl,
    )

    if not collected:
        print("[INFO] No posts collected, nothing to save.")
        input("Press Enter to exit...")
        return

    download_images_for_posts(collected, cookies=cookies or None)
    out_path = Path("fb_group_posts_selenium.csv")
    save_posts_to_csv(collected, out_path)
    print(f"[INFO] Saved results to {out_path.resolve()}")
    print("[INFO] Images (if any) are in the fb_images/ folder.")
    # For users who run the script by double-clicking the .py file on Windows,
    # keep the console window open so they can read the messages.
    try:
        input("Scrape finished. Press Enter to close this window...")
    except EOFError:
        # In environments without a real stdin (e.g., some schedulers), ignore.
        pass


# ------------------ Advanced Tkinter GUI wrapper ------------------ #

try:
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None  # UI will not be available


class AdvancedSeleniumScraperApp(tk.Tk):
    """
    Advanced GUI:
    - Inputs at the top (Group URL/ID, Keyword, Max posts, Cookies file)
    - Checkbox: Only posts with Sri Lankan phone number
    - Buttons: Start Scrape, Open Output Folder, Reload Results, Close
    - Results table with columns: Post URL, Post Text, Image Paths
    - Status line and progress bar at the top of the main area
    - Double-click a row to open the post in your web browser
    """

    def __init__(self):
        super().__init__()
        self.title("Facebook Group Selenium Scraper (Advanced)")
        self.geometry("950x620")
        self.minsize(850, 500)

        self.group_var = tk.StringVar()
        self.keyword_var = tk.StringVar()
        self.max_posts_var = tk.StringVar(value="50")
        self.cookies_var = tk.StringVar()
        self.only_sl_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Idle")

        self.data: List[Dict[str, str]] = []

        self._build_ui()

        # Improve default look-and-feel where possible
        try:
            style = ttk.Style(self)
            # Use a more modern theme if available
            for candidate in ("clam", "vista", "default"):
                if candidate in style.theme_names():
                    style.theme_use(candidate)
                    break
        except Exception:
            pass

    # ---------- UI construction ----------

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Group URL / ID:").grid(
            row=0, column=0, sticky="e", padx=5, pady=4
        )
        ttk.Entry(top, textvariable=self.group_var, width=60).grid(
            row=0, column=1, columnspan=3, sticky="we", pady=4
        )

        ttk.Label(top, text="Keyword (optional):").grid(
            row=1, column=0, sticky="e", padx=5, pady=4
        )
        ttk.Entry(top, textvariable=self.keyword_var, width=30).grid(
            row=1, column=1, sticky="we", pady=4
        )

        ttk.Label(top, text="Max posts:").grid(
            row=1, column=2, sticky="e", padx=5, pady=4
        )
        ttk.Entry(top, textvariable=self.max_posts_var, width=10).grid(
            row=1, column=3, sticky="w", pady=4
        )

        ttk.Label(top, text="Cookies file (cookies.txt):").grid(
            row=2, column=0, sticky="e", padx=5, pady=4
        )
        ttk.Entry(top, textvariable=self.cookies_var, width=45).grid(
            row=2, column=1, sticky="we", pady=4
        )
        ttk.Button(top, text="Browse...", command=self._on_browse_cookies).grid(
            row=2, column=2, sticky="w", padx=5, pady=4
        )

        ttk.Checkbutton(
            top,
            text="Only posts with Sri Lankan phone number",
            variable=self.only_sl_var,
        ).grid(row=3, column=1, columnspan=2, sticky="w", pady=4)

        ttk.Button(top, text="Start Scrape", command=self._on_start).grid(
            row=4, column=1, sticky="e", padx=5, pady=6
        )
        ttk.Button(top, text="Reload Results", command=self._on_reload_results).grid(
            row=4, column=2, sticky="w", padx=5, pady=6
        )
        ttk.Button(top, text="Open Output Folder", command=self._on_open_output).grid(
            row=4, column=3, sticky="w", padx=5, pady=6
        )

        for i in range(4):
            top.columnconfigure(i, weight=1)

        status_frame = ttk.Frame(self, padding=(10, 0))
        status_frame.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(status_frame, textvariable=self.status_var).pack(
            side=tk.LEFT, anchor="w"
        )

        # Progress bar to give visual feedback while scraping
        self.progress = ttk.Progressbar(
            status_frame,
            mode="indeterminate",
            length=220,
        )
        self.progress.pack(side=tk.RIGHT, padx=(5, 0), anchor="e")

        table_frame = ttk.Frame(self, padding=10)
        table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        columns = ("post_url", "post_text", "image_paths")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("post_url", text="Post URL")
        self.tree.heading("post_text", text="Post Text (first 300 chars)")
        self.tree.heading("image_paths", text="Image Paths")

        self.tree.column("post_url", width=260, anchor="w")
        self.tree.column("post_text", width=360, anchor="w")
        self.tree.column("image_paths", width=260, anchor="w")

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="we")

        # Double-click a row to open the post URL in the default web browser
        self.tree.bind("<Double-1>", self._on_open_selected_post)

        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(bottom, text="Close", command=self.destroy).pack(
            side=tk.RIGHT, padx=5
        )

    # ---------- Helpers ----------

    def _set_status(self, text: str):
        self.status_var.set(text)
        self.update_idletasks()

    def _start_progress(self):
        try:
            self.progress.start(10)
        except Exception:
            pass

    def _stop_progress(self):
        try:
            self.progress.stop()
            self.progress["value"] = 0
        except Exception:
            pass

    def _on_browse_cookies(self):
        path = filedialog.askopenfilename(
            title="Select cookies.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.cookies_var.set(path)

    def _on_start(self):
        group_input = self.group_var.get().strip()
        keyword = self.keyword_var.get().strip()
        max_posts_str = self.max_posts_var.get().strip() or "50"
        cookies_path_str = self.cookies_var.get().strip()
        only_sl = self.only_sl_var.get()

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

        cookies: List[Dict[str, str]] = []
        if cookies_path_str:
            cookies_path = Path(cookies_path_str)
            if not cookies_path.is_file():
                messagebox.showerror("Error", "Cookies file not found.")
                return
            try:
                cookies = load_netscape_cookies(cookies_path)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load cookies: {e}")
                return

        self._set_status("Running… please watch the browser window and console.")
        self._start_progress()
        t = threading.Thread(
            target=self._run_scrape_thread,
            args=(group_input, keyword, max_posts, cookies, only_sl),
            daemon=True,
        )
        t.start()

    def _run_scrape_thread(
        self,
        group_input: str,
        keyword: str,
        max_posts: int,
        cookies: List[Dict[str, str]],
        only_sl: bool,
    ):
        try:
            posts = selenium_collect_posts(
                group_input=group_input,
                keyword=keyword,
                max_posts=max_posts,
                cookies=cookies or None,
                only_sl_phones=only_sl,
            )
            if not posts:
                self.after(
                    0,
                    lambda: (
                        self._stop_progress(),
                        self._set_status("Done. No posts matched the filters."),
                        messagebox.showinfo(
                            "Scrape finished", "No posts matched the selected filters."
                        ),
                    ),
                )
                return

            download_images_for_posts(posts, cookies=cookies or None)
            out_path = Path("fb_group_posts_selenium.csv")
            save_posts_to_csv(posts, out_path)
            self.data = posts

            def update_ui():
                self._populate_table()
                self._stop_progress()
                self._set_status(
                    f"Done. Found {len(posts)} post(s). "
                    "Data saved to fb_group_posts_selenium.csv."
                )
                try:
                    messagebox.showinfo(
                        "Scrape finished",
                        f"Found {len(posts)} post(s).\n\n"
                        "Results saved to fb_group_posts_selenium.csv\n"
                        "Images (if any) are in the fb_images folder.\n\n"
                        "You can also double-click a row to open the post in your browser.",
                    )
                except Exception:
                    # Message boxes can fail in some edge cases; ignore quietly.
                    pass

            self.after(0, update_ui)
        except Exception as e:
            self.after(
                0,
                lambda: (
                    self._stop_progress(),
                    self._set_status("Error during scrape."),
                    messagebox.showerror("Error", str(e)),
                ),
            )

    def _populate_table(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        for p in self.data:
            post_url = p.get("post_url", "")
            text = (p.get("post_text", "") or "").replace("\n", " ")
            short_text = text[:300]
            image_paths = p.get("image_paths", "")
            self.tree.insert(
                "",
                "end",
                values=(post_url, short_text, image_paths),
            )

    def _on_reload_results(self):
        path = Path("fb_group_posts_selenium.csv")
        if not path.is_file():
            messagebox.showinfo(
                "Info", "fb_group_posts_selenium.csv not found in the current folder."
            )
            return

        try:
            data: List[Dict[str, str]] = []
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    data.append(row)
            self.data = data
            self._populate_table()
            self._set_status(
                f"Reloaded {len(data)} row(s) from fb_group_posts_selenium.csv."
            )
        except Exception as e:
            messagebox.showerror("Error", f"Failed to reload CSV: {e}")

    def _on_open_output(self):
        """
        Open the folder where the script (and CSV/images) live, not the OS user home.
        """
        # Use the directory of this script file as the output folder base.
        script_dir = Path(__file__).resolve().parent
        folder = script_dir

        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except Exception as e:
            messagebox.showerror("Error", f"Could not open folder: {e}")

    def _on_open_selected_post(self, event):
        """
        Double-click handler: open the selected post URL in the default browser.
        """
        selection = self.tree.selection()
        if not selection:
            return
        item_id = selection[0]
        values = self.tree.item(item_id, "values")
        if not values:
            return
        post_url = values[0]
        if not post_url:
            return
        try:
            webbrowser.open(post_url)
        except Exception:
            messagebox.showerror("Error", "Could not open the post URL in browser.")


def run_selenium_scrape(
    group_input: str,
    keyword: str,
    max_posts: int,
    cookies_path_str: str,
    only_sl_phones: bool = False,
):
    """
    Non-interactive wrapper usable from external code (kept for compatibility).
    It mirrors the CLI but does not show any GUI; outputs CSV and images.
    """
    cookies: List[Dict[str, str]] = []
    if cookies_path_str:
        cookies_path = Path(cookies_path_str)
        if not cookies_path.is_file():
            raise FileNotFoundError(f"Cookies file not found: {cookies_path}")
        cookies = load_netscape_cookies(cookies_path)
        print(f"[INFO] Loaded {len(cookies)} cookies from {cookies_path}")

    posts = selenium_collect_posts(
        group_input=group_input,
        keyword=keyword,
        max_posts=max_posts,
        cookies=cookies or None,
        only_sl_phones=only_sl_phones,
    )
    if not posts:
        print("[INFO] No posts collected, nothing to save.")
        return

    download_images_for_posts(posts, cookies=cookies or None)
    out_path = Path("fb_group_posts_selenium.csv")
    save_posts_to_csv(posts, out_path)
    print(f"[INFO] Saved results to {out_path.resolve()}")
    print("[INFO] Images (if any) are in the fb_images/ folder.")


if __name__ == "__main__":
    if tk is not None:
        app = AdvancedSeleniumScraperApp()
        app.mainloop()
    else:
        main()