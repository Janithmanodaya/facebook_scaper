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

# GUI imports (optional)
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None  # UI will not be available

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


def main_cli():
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


class SinglePostScraperApp(tk.Tk):
    """
    Simple GUI for scraping a single Facebook post by exact URL.
    - Input: Post URL, Cookies file (optional)
    - Buttons: Start Scrape, Open Output Folder, Close
    - Shows status and a small preview of description and image paths.
    """

    def __init__(self):
        super().__init__()
        self.title("Facebook Single Post Scraper")
        self.geometry("820x420")
        self.minsize(750, 380)

        self.post_url_var = tk.StringVar()
        self.cookies_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Idle")

        self.post_data: Optional[Dict[str, object]] = None

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

        ttk.Label(top, text="Facebook Post URL:").grid(
            row=0, column=0, sticky="e", padx=5, pady=4
        )
        ttk.Entry(top, textvariable=self.post_url_var, width=70).grid(
            row=0, column=1, columnspan=2, sticky="we", pady=4
        )

        ttk.Label(top, text="Cookies file (cookies.txt, optional):").grid(
            row=1, column=0, sticky="e", padx=5, pady=4
        )
        ttk.Entry(top, textvariable=self.cookies_var, width=50).grid(
            row=1, column=1, sticky="we", pady=4
        )
        ttk.Button(top, text="Browse...", command=self._on_browse_cookies).grid(
            row=1, column=2, sticky="w", padx=5, pady=4
        )

        ttk.Button(top, text="Start Scrape", command=self._on_start).grid(
            row=2, column=1, sticky="e", padx=5, pady=8
        )
        ttk.Button(top, text="Open Output Folder", command=self._on_open_output).grid(
            row=2, column=2, sticky="w", padx=5, pady=8
        )

        for i in range(3):
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

        preview_frame = ttk.LabelFrame(self, text="Post Preview", padding=10)
        preview_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)

        ttk.Label(preview_frame, text="URL:").grid(
            row=0, column=0, sticky="nw", padx=5, pady=4
        )
        self.url_label = ttk.Label(preview_frame, text="", wraplength=650)
        self.url_label.grid(row=0, column=1, sticky="w", padx=5, pady=4)

        ttk.Label(preview_frame, text="Description:").grid(
            row=1, column=0, sticky="nw", padx=5, pady=4
        )
        self.text_box = tk.Text(preview_frame, height=6, wrap="word")
        self.text_box.grid(row=1, column=1, sticky="nsew", padx=5, pady=4)

        ttk.Label(preview_frame, text="Image paths:").grid(
            row=2, column=0, sticky="nw", padx=5, pady=4
        )
        self.images_box = tk.Text(preview_frame, height=4, wrap="word")
        self.images_box.grid(row=2, column=1, sticky="nsew", padx=5, pady=4)

        preview_frame.columnconfigure(1, weight=1)
        preview_frame.rowconfigure(1, weight=1)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(bottom, text="Close", command=self.destroy).pack(
            side=tk.RIGHT, padx=5
        )

        self.text_box.configure(state="disabled")
        self.images_box.configure(state="disabled")

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
        post_url = self.post_url_var.get().strip()
        cookies_path_str = self.cookies_var.get().strip()

        if not post_url:
            messagebox.showerror("Error", "Please enter a Facebook post URL.")
            return

        self._set_status("Scraping… (headless browser, please wait)")
        self._start_progress()

        t = threading.Thread(
            target=self._run_scrape_thread,
            args=(post_url, cookies_path_str),
            daemon=True,
        )
        t.start()

    def _run_scrape_thread(self, post_url: str, cookies_path_str: str):
        try:
            post = run_single_post_scrape(
                post_url=post_url,
                cookies_path_str=cookies_path_str,
                headless=True,
            )
            if not post:
                self.after(
                    0,
                    lambda: (
                        self._stop_progress(),
                        self._set_status("Done, but no data extracted."),
                        messagebox.showinfo(
                            "Finished",
                            "Scraping finished but no data was extracted.\n"
                            "Check the console for details.",
                        ),
                    ),
                )
                return

            self.post_data = post

            def update_ui():
                self._populate_preview()
                self._stop_progress()
                self._set_status("Done.")
                try:
                    script_dir = Path(__file__).resolve().parent
                    csv_path = script_dir / "fb_single_post.csv"
                    images_path = script_dir / "fb_images"
                    messagebox.showinfo(
                        "Scrape finished",
                        f"Post scraped successfully.\n\n"
                        f"CSV saved to:\n{csv_path}\n\n"
                        f"Images (if any) are in:\n{images_path}",
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

    def _populate_preview(self):
        if not self.post_data:
            return

        url = str(self.post_data.get("post_url", ""))
        text = str(self.post_data.get("post_text", "") or "")
        image_paths = str(self.post_data.get("image_paths", "") or "")

        self.url_label.configure(text=url)

        self.text_box.configure(state="normal")
        self.text_box.delete("1.0", tk.END)
        self.text_box.insert("1.0", text)
        self.text_box.configure(state="disabled")

        self.images_box.configure(state="normal")
        self.images_box.delete("1.0", tk.END)
        self.images_box.insert("1.0", image_paths)
        self.images_box.configure(state="disabled")

    def _on_open_output(self):
        """
        Open the folder where the script (and CSV/images) live.
        """
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


if __name__ == "__main__":
    if tk is not None:
        app = SinglePostScraperApp()
        app.mainloop()
    else:
        main_cli()