import csv
import os
import re
import sys
import time
import webbrowser
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


# Facebook image URLs often contain long query strings – we keep them.
FB_IMAGE_URL_REGEX = re.compile(
    r"https?://[^\"'\\s]+?\\.(?:jpg|jpeg|png|webp)(?:\\?[^\"'\\s]*)?",
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
    Attach cookies to the browser for facebook.com, then continue.
    """
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


def extract_single_post_from_dom(driver: webdriver.Chrome) -> Optional[Dict[str, str]]:
    """
    Extract a single post (text + images) from the current Facebook page.

    Intended for:
    - Direct post URLs
    - Share URLs like /share/p/...
    """
    # Prefer role="article" containers.
    try:
        articles = driver.find_elements(By.XPATH, "//div[@role='article']")
    except Exception as e:
        print(f"[DEBUG] Failed to locate article containers: {e}")
        articles = []

    print(f"[DEBUG] Found {len(articles)} candidate article elements on the page.")

    if not articles:
        # Fallback: treat the whole body as one container.
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            articles = [body]
        except Exception as e:
            print(f"[DEBUG] Could not even get <body>: {e}")
            return None

    # Choose the first article that has visible text or images.
    chosen = None
    for art in articles:
        text = art.text or ""
        img_elements = []
        try:
            img_elements = art.find_elements(By.XPATH, ".//img")
        except Exception:
            img_elements = []
        if text.strip() or img_elements:
            chosen = art
            break

    if chosen is None:
        print("[DEBUG] No suitable article element with text or images found.")
        return None

    text = chosen.text or ""
    html = ""
    try:
        html = chosen.get_attribute("innerHTML") or ""
    except Exception:
        html = ""

    # If text is empty but html exists, strip tags to get a rough plain text.
    if not text and html:
        rough = re.sub(r"<[^>]+>", " ", html)
        text = " ".join(rough.split())

    image_urls: List[str] = []
    try:
        img_elements = chosen.find_elements(By.XPATH, ".//img")
        for img in img_elements:
            src = img.get_attribute("src") or ""
            if not src:
                continue
            if src.startswith("data:"):
                # Skip inline SVG/icons.
                continue
            if src not in image_urls:
                image_urls.append(src)
    except Exception:
        pass

    if html:
        # Also scan the raw HTML for any direct image URLs (fbcdn, scontent, etc.).
        for match in FB_IMAGE_URL_REGEX.findall(html):
            clean_url = html_lib.unescape(match)
            if clean_url not in image_urls:
                image_urls.append(clean_url)

    current_url = ""
    try:
        current_url = driver.current_url or ""
    except Exception:
        current_url = ""

    print(
        f"[DEBUG] Single post extraction: text_len={len(text)}, "
        f"images={len(image_urls)}"
    )

    return {
        "post_url": current_url,
        "post_text": text[:4000],
        "html": html,
        "image_urls": image_urls,
    }


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

    script_dir = Path(__file__).resolve().parent
    img_dir = script_dir / "fb_images"
    img_dir.mkdir(exist_ok=True)

    headers_base: Dict[str, str] = {}
    if cookies:
        cookie_header = build_cookie_header(cookies)
        if cookie_header:
            headers_base["Cookie"] = cookie_header

    headers_base.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    headers_base.setdefault(
        "Accept", "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    )
    headers_base.setdefault("Accept-Language", "en-US,en;q=0.9")
    headers_base.setdefault("Connection", "keep-alive")

    for i, post in enumerate(posts, start=1):
        image_urls = post.get("image_urls") or []
        local_paths: List[str] = []

        if not image_urls:
            print(
                f"[DEBUG] Post #{i} ({post.get('post_url','')}) has no image URLs "
                f"to download."
            )
            post["image_paths"] = ""
            continue

        post_url = post.get("post_url", "") or "https://www.facebook.com/"
        headers = dict(headers_base)
        headers["Referer"] = post_url

        for j, url in enumerate(image_urls, start=1):
            if url.startswith("data:"):
                print(f"[DEBUG] Skipping inline image data URI for post {i}")
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


def selenium_collect_single_post(
    post_input: str,
    cookies: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    """
    Open a single Facebook post URL and extract:
    - post_url
    - post_text
    - image_urls

    This is the ONLY scraping mode now (no groups, no keyword filters).
    """
    post_url = (post_input or "").strip()
    if not post_url:
        print("[INFO] Empty post URL given.")
        return []

    print(f"[INFO] Opening Facebook post URL: {post_url}")

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
            driver.get(post_url)
            print("[INFO] Browser opened with your cookies applied.")
        else:
            print("[INFO] No cookies provided. A browser window will open.")
            driver.get(post_url)

        # Give the page some time to fully load.
        time.sleep(8)

        post = extract_single_post_from_dom(driver)
        if not post:
            print("[INFO] No post content found on the page.")
            return []

        # Force URL to the exact URL that user supplied.
        post["post_url"] = post_url

        print("[INFO] Single post content extracted successfully.")
        return [post]

    finally:
        driver.quit()


def main():
    print("=== Selenium Facebook Single Post Scraper ===")
    print("Paste an exact Facebook POST or SHARE URL.")
    print()

    post_input = input("Enter Facebook post URL: ").strip()
    cookies_path_str = input(
        "Path to cookies.txt (recommended, Enter to skip): "
    ).strip()

    cookies: List[Dict[str, str]] = []
    if cookies_path_str:
        cookies_path = Path(cookies_path_str)
        if not cookies_path.is_file():
            print(f"Cookies file not found: {cookies_path}")
            input("Press Enter to exit...")
            return
        cookies = load_netscape_cookies(cookies_path)
        print(f"[INFO] Loaded {len(cookies)} cookies from {cookies_path}")

    posts = selenium_collect_single_post(
        post_input=post_input,
        cookies=cookies or None,
    )

    if not posts:
        print("[INFO] No post content extracted, nothing to save.")
        try:
            input("Press Enter to exit...")
        except EOFError:
            pass
        return

    download_images_for_posts(posts, cookies=cookies or None)

    script_dir = Path(__file__).resolve().parent
    out_path = script_dir / "fb_post_selenium.csv"

    save_posts_to_csv(posts, out_path)
    print(f"[INFO] Saved result to {out_path}")
    print(f"[INFO] Images (if any) are in: {script_dir / 'fb_images'}")
    print(f"[INFO] You can open this folder in Explorer: {script_dir}")

    try:
        input("Scrape finished. Press Enter to close this window...")
    except EOFError:
        pass


# ------------------ Simple Tkinter GUI wrapper (single post only) ------------------ #

try:
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None  # UI will not be available


class SinglePostScraperApp(tk.Tk):
    """
    GUI for scraping a SINGLE Facebook post URL.
    - Fields:
      * Post URL
      * Cookies file
    - Buttons:
      * Start Scrape
      * Reload Last Result
      * Open Output Folder
      * Close
    - Table:
      * Post URL
      * Post Text (first 300 chars)
      * Image Paths
    """

    def __init__(self):
        super().__init__()
        self.title("Facebook Single Post Scraper")
        self.geometry("900x600")
        self.minsize(800, 450)

        self.post_var = tk.StringVar()
        self.cookies_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Idle")

        self.data: List[Dict[str, str]] = []

        self._build_ui()

        try:
            style = ttk.Style(self)
            for candidate in ("clam", "vista", "default"):
                if candidate in style.theme_names():
                    style.theme_use(candidate)
                    break
        except Exception:
            pass

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Post URL:").grid(
            row=0, column=0, sticky="e", padx=5, pady=4
        )
        ttk.Entry(top, textvariable=self.post_var, width=60).grid(
            row=0, column=1, columnspan=3, sticky="we", pady=4
        )

        ttk.Label(top, text="Cookies file (cookies.txt):").grid(
            row=1, column=0, sticky="e", padx=5, pady=4
        )
        ttk.Entry(top, textvariable=self.cookies_var, width=45).grid(
            row=1, column=1, sticky="we", pady=4
        )
        ttk.Button(top, text="Browse...", command=self._on_browse_cookies).grid(
            row=1, column=2, sticky="w", padx=5, pady=4
        )

        ttk.Button(top, text="Start Scrape", command=self._on_start).grid(
            row=2, column=1, sticky="e", padx=5, pady=6
        )
        ttk.Button(top, text="Reload Result", command=self._on_reload_results).grid(
            row=2, column=2, sticky="w", padx=5, pady=6
        )
        ttk.Button(top, text="Open Output Folder", command=self._on_open_output).grid(
            row=2, column=3, sticky="w", padx=5, pady=6
        )

        for i in range(4):
            top.columnconfigure(i, weight=1)

        status_frame = ttk.Frame(self, padding=(10, 0))
        status_frame.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(status_frame, textvariable=self.status_var).pack(
            side=tk.LEFT, anchor="w"
        )

        self.progress = ttk.Progressbar(
            status_frame,
            mode="indeterminate",
            length=200,
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

        self.tree.bind("<Double-1>", self._on_open_selected_post)

        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(bottom, text="Close", command=self.destroy).pack(
            side=tk.RIGHT, padx=5
        )

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
        post_input = self.post_var.get().strip()
        cookies_path_str = self.cookies_var.get().strip()

        if not post_input:
            messagebox.showerror("Error", "Please enter a Facebook post URL.")
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
            args=(post_input, cookies),
            daemon=True,
        )
        t.start()

    def _run_scrape_thread(
        self,
        post_input: str,
        cookies: List[Dict[str, str]],
    ):
        try:
            posts = selenium_collect_single_post(
                post_input=post_input,
                cookies=cookies or None,
            )
            if not posts:
                self.after(
                    0,
                    lambda: (
                        self._stop_progress(),
                        self._set_status("Done. No post content detected."),
                        messagebox.showinfo(
                            "Scrape finished",
                            "No post content could be extracted from this URL.",
                        ),
                    ),
                )
                return

            download_images_for_posts(posts, cookies=cookies or None)

            script_dir = Path(__file__).resolve().parent
            out_path = script_dir / "fb_post_selenium.csv"

            save_posts_to_csv(posts, out_path)
            self.data = posts

            def update_ui():
                self._populate_table()
                self._stop_progress()

                csv_path = out_path
                images_path = script_dir / "fb_images"

                self._set_status(
                    f"Done. Extracted post content. Data saved to {csv_path.name}."
                )
                try:
                    messagebox.showinfo(
                        "Scrape finished",
                        f"Post content saved to:\n{csv_path}\n\n"
                        f"Images (if any) are in:\n{images_path}\n\n"
                        "You can double-click the row to open the post in your browser.",
                    )
                except Exception:
                    pass

            self.after(0, update_ui)
        except Exception as e:
            error_message = str(e)
            self.after(
                0,
                lambda msg=error_message: (
                    self._stop_progress(),
                    self._set_status("Error during scrape."),
                    messagebox.showerror("Error", msg),
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
        script_dir = Path(__file__).resolve().parent
        path = script_dir / "fb_post_selenium.csv"
        if not path.is_file():
            messagebox.showinfo(
                "Info",
                f"{path.name} not found in:\n{script_dir}",
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
                f"Reloaded {len(data)} row(s) from fb_post_selenium.csv."
            )
        except Exception as e:
            messagebox.showerror("Error", f"Failed to reload CSV: {e}")

    def _on_open_output(self):
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
    post_input: str,
    cookies_path_str: str,
):
    """
    Non-interactive helper: scrape a single post, save CSV and images.
    """
    cookies: List[Dict[str, str]] = []
    if cookies_path_str:
        cookies_path = Path(cookies_path_str)
        if not cookies_path.is_file():
            raise FileNotFoundError(f"Cookies file not found: {cookies_path}")
        cookies = load_netscape_cookies(cookies_path)
        print(f"[INFO] Loaded {len(cookies)} cookies from {cookies_path}")

    posts = selenium_collect_single_post(
        post_input=post_input,
        cookies=cookies or None,
    )
    if not posts:
        print("[INFO] No post content extracted, nothing to save.")
        return

    download_images_for_posts(posts, cookies=cookies or None)

    script_dir = Path(__file__).resolve().parent
    out_path = script_dir / "fb_post_selenium.csv"

    save_posts_to_csv(posts, out_path)
    print(f"[INFO] Saved result to {out_path}")
    print(f"[INFO] Images (if any) are in: {script_dir / 'fb_images'}")


if __name__ == "__main__":
    if tk is not None:
        app = SinglePostScraperApp()
        app.mainloop()
    else:
        main():
    print("=== Selenium Facebook Scraper (experimental) ===")
    print("You can enter either:")
    print(" - A Facebook GROUP URL or ID  (to collect multiple posts), OR")
    print(" - A Facebook POST URL        (to collect just that single post).")
    print()

    group_input = input("Enter Facebook group URL/ID or single post URL: ").strip()
    is_single_post_mode = ("/posts/" in group_input) and ("/groups/" not in group_input)

    keyword = ""
    max_posts = 1
    sl_only_str = "n"

    if not is_single_post_mode:
        # Group mode: ask for filters and limits
        keyword = input("Enter keyword to filter by (leave empty for all): ").strip()
        max_posts_str = input("Max posts to save (default 50): ").strip() or "50"
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
    cookies_path_str = input(
        "Path to cookies.txt (recommended, enter to skip): "
    ).strip()
    if cookies_path_str:
        cookies_path = Path(cookies_path_str)
        if not cookies_path.is_file():
            print(f"Cookies file not found: {cookies_path}")
            input("Press Enter to exit...")
            return
        cookies = load_netscape_cookies(cookies_path)
        print(f"[INFO] Loaded {len(cookies)} cookies from {cookies_path}")

    only_sl = sl_only_str in {"y", "yes"}

    if is_single_post_mode:
        collected = selenium_collect_single_post(
            post_input=group_input,
            cookies=cookies or None,
        )
    else:
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

    # Always save CSV next to this script so the folder is predictable
    script_dir = Path(__file__).resolve().parent
    out_path = script_dir / "fb_group_posts_selenium.csv"

    save_posts_to_csv(collected, out_path)
    print(f"[INFO] Saved results to {out_path}")
    print(f"[INFO] Images (if any) are in: {script_dir / 'fb_images'}")
    print(f"[INFO] You can open this folder in Explorer: {script_dir}")

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
    - You can scrape either:
      * Multiple posts from a Facebook GROUP (URL/ID), or
      * A single Facebook POST (direct post URL)
    - Inputs at the top (Group/Post URL, optional Keyword & Max posts for groups, Cookies file)
    - Checkbox: Only posts with Sri Lankan phone number (group mode only)
    - Buttons: Start Scrape, Open Output Folder, Reload Results, Close
    - Results table with columns: Post URL, Post Text, Image Paths
    - Status line and progress bar at the top of the main area
    - Double-click a row to open the post in your web browser
    """

    def __init__(self):
        super().__init__()
        self.title("Facebook Selenium Scraper (Group or Single Post)")
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

        ttk.Label(top, text="Group / Post URL:").grid(
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
            messagebox.showerror("Error", "Please enter a group or post URL.")
            return

        is_single_post_mode = ("/posts/" in group_input) and ("/groups/" not in group_input)

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
            args=(group_input, keyword, max_posts, cookies, only_sl, is_single_post_mode),
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
        is_single_post_mode: bool,
    ):
        try:
            if is_single_post_mode:
                posts = selenium_collect_single_post(
                    post_input=group_input,
                    cookies=cookies or None,
                )
            else:
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

            # Save CSV next to this script so the location is always clear
            script_dir = Path(__file__).resolve().parent
            out_path = script_dir / "fb_group_posts_selenium.csv"

            save_posts_to_csv(posts, out_path)
            self.data = posts

            def update_ui():
                self._populate_table()
                self._stop_progress()

                script_dir = Path(__file__).resolve().parent
                csv_path = script_dir / "fb_group_posts_selenium.csv"
                images_path = script_dir / "fb_images"

                self._set_status(
                    f"Done. Found {len(posts)} post(s). Data saved to {csv_path.name}."
                )
                try:
                    messagebox.showinfo(
                        "Scrape finished",
                        f"Found {len(posts)} post(s).\n\n"
                        f"Results saved to:\n{csv_path}\n\n"
                        f"Images (if any) are in:\n{images_path}\n\n"
                        "You can also double-click a row to open the post in your browser.",
                    )
                except Exception:
                    # Message boxes can fail in some edge cases; ignore quietly.
                    pass

            self.after(0, update_ui)
        except Exception as e:
            error_message = str(e)
            self.after(
                0,
                lambda msg=error_message: (
                    self._stop_progress(),
                    self._set_status("Error during scrape."),
                    messagebox.showerror("Error", msg),
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
        # Reload the CSV from the same folder as this script
        script_dir = Path(__file__).resolve().parent
        path = script_dir / "fb_group_posts_selenium.csv"
        if not path.is_file():
            messagebox.showinfo(
                "Info",
                f"{path.name} not found in:\n{script_dir}",
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
    fb_input: str,
    keyword: str,
    max_posts: int,
    cookies_path_str: str,
    only_sl_phones: bool = False,
):
    """
    Non-interactive wrapper usable from external code (kept for compatibility).

    - If fb_input looks like a single POST URL (contains "/posts/" but not "/groups/"),
      it will scrape just that post (no keyword / phone filters).
    - Otherwise, it behaves like the legacy group scraper.
    """
    cookies: List[Dict[str, str]] = []
    if cookies_path_str:
        cookies_path = Path(cookies_path_str)
        if not cookies_path.is_file():
            raise FileNotFoundError(f"Cookies file not found: {cookies_path}")
        cookies = load_netscape_cookies(cookies_path)
        print(f"[INFO] Loaded {len(cookies)} cookies from {cookies_path}")

    is_single_post_mode = ("/posts/" in fb_input) and ("/groups/" not in fb_input)

    if is_single_post_mode:
        posts = selenium_collect_single_post(
            post_input=fb_input,
            cookies=cookies or None,
        )
    else:
        posts = selenium_collect_posts(
            group_input=fb_input,
            keyword=keyword,
            max_posts=max_posts,
            cookies=cookies or None,
            only_sl_phones=only_sl_phones,
        )
    if not posts:
        print("[INFO] No posts collected, nothing to save.")
        return

    download_images_for_posts(posts, cookies=cookies or None)

    # Save CSV next to this script
    script_dir = Path(__file__).resolve().parent
    out_path = script_dir / "fb_group_posts_selenium.csv"

    save_posts_to_csv(posts, out_path)
    print(f"[INFO] Saved results to {out_path}")
    print(f"[INFO] Images (if any) are in: {script_dir / 'fb_images'}")


if __name__ == "__main__":
    if tk is not None:
        app = AdvancedSeleniumScraperApp()
        app.mainloop()
    else:
        main()