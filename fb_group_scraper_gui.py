import sys
import subprocess
import importlib
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path

# ------------------ Dependency check (no auto-install) ------------------ #

try:
    from facebook_scraper import get_posts  # type: ignore  # noqa: E402
except ImportError:
    print("The 'facebook_scraper' package is not installed.")
    print("Please install it by running:")
    print("    python -m pip install facebook_scraper")
    raise

try:
    import pandas as pd  # type: ignore  # noqa: E402
except pkg in REQUIRED_PACKAGES:
        ensure_package(pkg)


ensure_dependencies()
from facebook_scraper import get_posts  # type: ignore  # noqa: E402
import pandas as pd  # type: ignore  # noqa: E402

# ------------------ Scraping logic ------------------ #


def scrape_group_posts(group, keyword, max_posts=100, cookies_file=None):
    """
    Scrape posts from a Facebook group with facebook_scraper.

    Parameters:
    - group: group ID or group URL (facebook_scraper accepts both)
    - keyword: keyword to filter by in post_text (case-insensitive)
    - max_posts: maximum number of matched posts to return
    - cookies_file: path to cookies.txt file (optional)

    Returns list of dicts:
    - post_text
    - post_url
    - shared_text
    - shared_link
    - time
    """
    keyword = (keyword or "").strip().lower()
    use_filter = bool(keyword)

    result = []
    count = 0

    for post in get_posts(
        group=group,
        pages=1000,
        cookies=cookies_file if cookies_file else None,
        options={"allow_extra_requests": False},
    ):
        text = (post.get("post_text") or "").strip()
        shared_text = (post.get("shared_text") or "").strip()

        full_text = (text + " " + shared_text).lower()

        if use_filter and keyword not in full_text:
            continue

        record = {
            "time": str(post.get("time") or ""),
            "post_text": text,
            "shared_text": shared_text,
            "post_url": post.get("post_url") or "",
            "shared_link": post.get("shared_link") or "",
        }
        result.append(record)
        count += 1

        if count >= max_posts:
            break

    return result


# ------------------ UI Application (Tkinter) ------------------ #


class FacebookScraperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Facebook Group Keyword Scraper (HTML-based)")
        self.geometry("950x620")
        self.minsize(850, 500)

        self.style = ttk.Style(self)
        self._configure_styles()

        self.group_var = tk.StringVar()
        self.keyword_var = tk.StringVar()
        self.limit_var = tk.StringVar(value="100")
        self.cookies_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Idle")

        self.data = []

        self._build_ui()

    def _configure_styles(self):
        self.style.theme_use("clam")
        self.style.configure("TLabel", font=("Segoe UI", 10))
        self.style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=6)
        self.style.configure("TEntry", font=("Segoe UI", 10))
        self.style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))
        self.style.configure("Status.TLabel", font=("Segoe UI", 9), foreground="#555555")
        self.style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(side=tk.TOP, fill=tk.X)

        header_lbl = ttk.Label(
            top, text="Facebook Group Keyword Scraper", style="Header.TLabel"
        )
        header_lbl.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        ttk.Label(top, text="Group ID / URL:").grid(
            row=1, column=0, sticky="e", padx=5, pady=4
        )
        group_entry = ttk.Entry(top, textvariable=self.group_var, width=50)
        group_entry.grid(row=1, column=1, columnspan=3, sticky="we", pady=4)

        ttk.Label(top, text="Keyword:").grid(
            row=2, column=0, sticky="e", padx=5, pady=4
        )
        keyword_entry = ttk.Entry(top, textvariable=self.keyword_var, width=30)
        keyword_entry.grid(row=2, column=1, sticky="we", pady=4)

        ttk.Label(top, text="Max matched posts:").grid(
            row=2, column=2, sticky="e", padx=5, pady=4
        )
        limit_entry = ttk.Entry(top, textvariable=self.limit_var, width=10)
        limit_entry.grid(row=2, column=3, sticky="w", pady=4)

        ttk.Label(top, text="Cookies file (optional):").grid(
            row=3, column=0, sticky="e", padx=5, pady=4
        )
        cookies_entry = ttk.Entry(top, textvariable=self.cookies_var, width=40)
        cookies_entry.grid(row=3, column=1, sticky="we", pady=4)
        cookies_btn = ttk.Button(
            top, text="Browse...", command=self.on_browse_cookies
        )
        cookies_btn.grid(row=3, column=2, sticky="w", padx=5, pady=4)

        scrape_btn = ttk.Button(top, text="Start Scrape", command=self.on_scrape_clicked)
        scrape_btn.grid(row=4, column=2, sticky="e", padx=5, pady=4)

        export_btn = ttk.Button(
            top, text="Export to CSV", command=self.on_export_clicked
        )
        export_btn.grid(row=4, column=3, sticky="w", padx=5, pady=4)

        for i in range(4):
            top.columnconfigure(i, weight=1)

        status_frame = ttk.Frame(self, padding=(10, 0))
        status_frame.pack(side=tk.TOP, fill=tk.X)
        status_lbl = ttk.Label(
            status_frame, textvariable=self.status_var, style="Status.TLabel"
        )
        status_lbl.pack(side=tk.LEFT)

        table_frame = ttk.Frame(self, padding=10)
        table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        columns = ("time", "post_text", "post_url", "shared_text", "shared_link")
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.heading("time", text="Time")
        self.tree.heading("post_text", text="Post Text")
        self.tree.heading("post_url", text="Post URL")
        self.tree.heading("shared_text", text="Shared Text")
        self.tree.heading("shared_link", text="Shared Link")

        self.tree.column("time", width=130, anchor="w")
        self.tree.column("post_text", width=360, anchor="w")
        self.tree.column("post_url", width=220, anchor="w")
        self.tree.column("shared_text", width=260, anchor="w")
        self.tree.column("shared_link", width=260, anchor="w")

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="we")

        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

    def set_status(self, text):
        self.status_var.set(text)
        self.update_idletasks()

    def on_browse_cookies(self):
        filename = filedialog.asksopenfilename(
            title="Select cookies.txt file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if filename:
            self.cookies_var.set(filename)

    def on_scrape_clicked(self):
        group = self.group_var.get().strip()
        keyword = self.keyword_var.get().strip()
        limit_str = self.limit_var.get().strip() or "100"
        cookies_file = self.cookies_var.get().strip() or None

        if not group:
            messagebox.showerror("Error", "Please enter Group ID or Group URL.")
            return

        try:
            max_posts = int(limit_str)
            if max_posts <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Error", "Max matched posts must be a positive integer."
            )
            return

        if cookies_file and not Path(cookies_file).is_file():
            messagebox.showerror("Error", "Cookies file not found.")
            return

        thread = threading.Thread(
            target=self._scrape_worker,
            args=(group, keyword, max_posts, cookies_file),
            daemon=True,
        )
        thread.start()

    def _scrape_worker(self, group, keyword, max_posts, cookies_file):
        try:
            self.set_status("Scraping posts... (this can take some time)")
            data = scrape_group_posts(
                group=group,
                keyword=keyword,
                max_posts=max_posts,
                cookies_file=cookies_file,
            )
            self.data = data
            self._update_table()
            self.set_status(f"Done. Found {len(data)} matching post(s).")
        except Exception as e:
            self.set_status("Error during scrape.")
            messagebox.showerror("Error", str(e))

    def _update_table(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        for item in self.data:
            self.tree.insert(
                "",
                "end",
                values=(
                    item.get("time", ""),
                    (item.get("post_text", "") or "")[:200].replace("\n", " "),
                    item.get("post_url", ""),
                    (item.get("shared_text", "") or "")[:160].replace("\n", " "),
                    item.get("shared_link", ""),
                ),
            )

    def on_export_clicked(self):
        if not self.data:
            messagebox.showinfo("Info", "No data to export. Run a scrape first.")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="Save CSV",
        )
        if not file_path:
            return

        try:
            df = pd.DataFrame(self.data)
            df.to_csv(file_path, index=False, encoding="utf-8-sig")
            messagebox.showinfo("Success", f"Exported to {file_path}")
        except Exception as e:
            messagebox.showerror("Error", str(e))


def main():
    app = FacebookScraperApp()
    app.mainloop()


if __name__ == "__main__":
    main()