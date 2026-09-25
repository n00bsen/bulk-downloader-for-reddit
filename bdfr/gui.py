#!/usr/bin/env python3

"""A small desktop front end for BDFR.

This replaces the hand-written batch launcher: instead of typing one username
and waiting for it to finish, several usernames can be queued and downloaded at
the same time, each in its own process, with the output folder and every option
editable in the window.

Tkinter is used deliberately: it ships with Python, needs no server and no
browser, and runs as an ordinary Windows desktop application.
"""

import json
import logging
import tkinter as tk
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import appdirs

from bdfr.configuration import Configuration
from bdfr.constants import DEFAULT_CONCURRENCY, DEFAULT_MAX_PARALLEL_JOBS, OAUTH_REDIRECT_URI
from bdfr.jobs import JobManager, JobState
from bdfr.locking import atomic_write

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = "gui_settings.json"
POLL_INTERVAL_MS = 200
SORT_CHOICES = ("hot", "top", "new", "controversial", "rising")
# Lower bounds for the window's minimum size, in pixels; the real minimum is
# measured from the widgets and can only be larger.
MIN_WINDOW_WIDTH = 920
MIN_WINDOW_HEIGHT = 580
# Height kept for the download list at minimum size: its heading plus a few rows.
MIN_JOB_TABLE_HEIGHT = 150
# Screen height the window leaves free for the taskbar and its own title bar.
SCREEN_HEIGHT_ALLOWANCE = 100


def fit_height_to_screen(wanted: int, screen_height: int, min_height: int) -> int | None:
    """Return a shorter initial window height if `wanted` does not fit on the screen.

    None means the natural height fits. The result never goes below the window's
    minimum size, at which every option is still shown in full.
    """
    available = screen_height - SCREEN_HEIGHT_ALLOWANCE
    if wanted <= available:
        return None
    return max(min_height, available)


def settings_path() -> Path:
    return Path(appdirs.AppDirs("bdfr", "BDFR").user_config_dir, SETTINGS_FILENAME)


@dataclass
class GuiSettings:
    """Everything the window remembers between runs.

    The defaults reproduce the batch file this window replaces, so the first
    launch behaves exactly like the old workflow.
    """

    directory: str = "D:\\reddit"
    # "user" downloads each name as a redditor, "subreddit" as a subreddit.
    source_type: str = "user"
    usernames: str = ""
    sort: str = "new"
    submitted: bool = True
    saved: bool = False
    upvoted: bool = False
    authenticate: bool = False
    no_dupes: bool = True
    # Off by default: a re-run then skips every post already downloaded in full
    # before any network work, which is what makes re-running a user cheap.
    recheck: bool = False
    skip: str = "gif, avi"
    folder_scheme: str = "{REDDITOR}"
    file_scheme: str = "{REDDITOR}_{TITLE}_{POSTID}"
    limit: str = ""
    concurrency: int = DEFAULT_CONCURRENCY
    max_parallel_jobs: int = DEFAULT_MAX_PARALLEL_JOBS
    # The user's own Reddit app; blank means the app in the BDFR config file,
    # which is the bundled, shared one unless the user has changed it. The
    # secret is kept in plaintext in this JSON file in the user's app-data
    # folder, just as config.cfg beside it already stores client secrets and
    # refresh tokens.
    client_id: str = ""
    # Out of the repr so that a log line or a failed test assertion that prints
    # the settings does not print the secret; asdict() still saves it.
    client_secret: str = field(default="", repr=False)
    reddit_username: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "GuiSettings":
        path = path or settings_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A missing or unreadable settings file is not an error; fall back
            # to the defaults rather than refusing to start.
            return cls()
        if not isinstance(raw, dict):
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in raw.items() if key in known})

    def save(self, path: Path | None = None) -> None:
        path = path or settings_path()
        try:
            atomic_write(path, json.dumps(asdict(self), indent=2))
        except OSError as e:
            logger.warning(f"Could not save GUI settings to {path}: {e}")


def parse_usernames(raw: str) -> list[str]:
    """Split the username box into a de-duplicated, order-preserving list.

    Newlines, commas and semicolons all separate entries so that a pasted list
    works however it happens to be formatted.
    """
    separators = str.maketrans({",": "\n", ";": "\n"})
    seen: dict[str, None] = {}
    for line in raw.translate(separators).splitlines():
        name = line.strip().strip("/")
        if name.lower().startswith("u/"):
            name = name[2:]
        if name:
            seen.setdefault(name, None)
    return list(seen)


def parse_skip_list(raw: str) -> list[str]:
    return [item.strip().lstrip(".") for item in raw.replace(";", ",").split(",") if item.strip()]


def build_job_configs(settings: GuiSettings) -> list[tuple[str, Configuration]]:
    """Turn the window state into one Configuration per username.

    Each job gets its own Configuration object because it is sent to a separate
    process; sharing one would let the processes see each other's mutations.
    """
    usernames = parse_usernames(settings.usernames)
    skip = parse_skip_list(settings.skip)
    limit = None
    if settings.limit.strip():
        try:
            limit = int(settings.limit.strip())
        except ValueError:
            logger.warning(f"Ignoring unparseable limit {settings.limit!r}")

    # Blank means "use the app in the config file". A secret is only meaningful with the
    # client ID it was issued for, so without an ID it is not passed on.
    client_id = settings.client_id.strip() or None
    client_secret = (settings.client_secret.strip() or None) if client_id else None
    reddit_username = settings.reddit_username.strip() or None

    jobs: list[tuple[str, Configuration]] = []
    for username in usernames:
        config = Configuration()
        config.directory = settings.directory
        if settings.source_type == "subreddit":
            config.subreddit = [username]
        else:
            config.user = [username]
            config.submitted = settings.submitted
            config.saved = settings.saved
            config.upvoted = settings.upvoted
        config.sort = settings.sort
        config.authenticate = settings.authenticate
        config.no_dupes = settings.no_dupes
        config.recheck = settings.recheck
        config.skip = skip
        config.folder_scheme = settings.folder_scheme
        config.file_scheme = settings.file_scheme
        config.limit = limit
        config.concurrency = settings.concurrency
        config.client_id = client_id
        config.client_secret = client_secret
        config.reddit_username = reddit_username
        # Left unset on purpose: the connector then picks a per-process log
        # file, which is what lets several jobs run at once.
        config.log = None
        jobs.append((username, config))
    return jobs


def validate_settings(settings: GuiSettings) -> str | None:
    """Return a human-readable problem, or None if the settings can be run."""
    noun = "subreddit" if settings.source_type == "subreddit" else "username"
    if not parse_usernames(settings.usernames):
        return f"Enter at least one {noun}, one per line."
    if not settings.directory.strip():
        return "Choose an output folder."
    if settings.client_secret.strip() and not settings.client_id.strip():
        return "Enter the Client ID that goes with the client secret, or clear the secret."
    if settings.source_type == "subreddit":
        # Submitted/Saved/Upvoted are redditor listings and do not apply here.
        return None
    if not any((settings.submitted, settings.saved, settings.upvoted)):
        return "Select at least one of Submitted, Saved or Upvoted."
    if (settings.saved or settings.upvoted) and not settings.authenticate:
        return "Saved and Upvoted posts require Authenticate to be enabled."
    return None


class BdfrGui:
    """The main window."""

    def __init__(self, root: tk.Tk, settings: GuiSettings | None = None):
        self.root = root
        self.settings = settings or GuiSettings.load()
        self.manager: JobManager | None = None
        self._row_for_job: dict[str, str] = {}

        root.title("BDFR Downloader")
        self._build_variables()
        self._build_layout()
        self._apply_minimum_size()
        self._schedule_poll()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------------- state

    def _build_variables(self) -> None:
        s = self.settings
        self.var_directory = tk.StringVar(value=s.directory)
        self.var_source_type = tk.StringVar(value=s.source_type)
        self.var_sort = tk.StringVar(value=s.sort)
        self.var_submitted = tk.BooleanVar(value=s.submitted)
        self.var_saved = tk.BooleanVar(value=s.saved)
        self.var_upvoted = tk.BooleanVar(value=s.upvoted)
        self.var_authenticate = tk.BooleanVar(value=s.authenticate)
        self.var_no_dupes = tk.BooleanVar(value=s.no_dupes)
        self.var_recheck = tk.BooleanVar(value=s.recheck)
        self.var_skip = tk.StringVar(value=s.skip)
        self.var_folder_scheme = tk.StringVar(value=s.folder_scheme)
        self.var_file_scheme = tk.StringVar(value=s.file_scheme)
        self.var_limit = tk.StringVar(value=s.limit)
        self.var_concurrency = tk.IntVar(value=s.concurrency)
        self.var_max_parallel = tk.IntVar(value=s.max_parallel_jobs)
        self.var_client_id = tk.StringVar(value=s.client_id)
        self.var_client_secret = tk.StringVar(value=s.client_secret)
        self.var_reddit_username = tk.StringVar(value=s.reddit_username)
        self.var_status = tk.StringVar(value="Ready")

    def collect_settings(self) -> GuiSettings:
        return GuiSettings(
            directory=self.var_directory.get(),
            source_type=self.var_source_type.get(),
            usernames=self.text_usernames.get("1.0", tk.END),
            sort=self.var_sort.get(),
            submitted=self.var_submitted.get(),
            saved=self.var_saved.get(),
            upvoted=self.var_upvoted.get(),
            authenticate=self.var_authenticate.get(),
            no_dupes=self.var_no_dupes.get(),
            recheck=self.var_recheck.get(),
            skip=self.var_skip.get(),
            folder_scheme=self.var_folder_scheme.get(),
            file_scheme=self.var_file_scheme.get(),
            limit=self.var_limit.get(),
            concurrency=self.var_concurrency.get(),
            max_parallel_jobs=self.var_max_parallel.get(),
            client_id=self.var_client_id.get(),
            client_secret=self.var_client_secret.get(),
            reddit_username=self.var_reddit_username.get(),
        )

    # --------------------------------------------------------------- layout

    def _build_layout(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        top = self.top_frame = ttk.Frame(root, padding=10)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1, minsize=280)
        top.columnconfigure(1, weight=2)

        self._build_options_panel(top)
        self._build_users_panel(top)
        self._build_actions(root)
        self._build_job_table(root)
        self._on_source_changed()

    def _apply_minimum_size(self) -> None:
        """Set the smallest window size at which no option is cut off.

        It is measured rather than fixed because a fixed size goes stale as soon
        as a row is added or Windows display scaling enlarges the font. Only the
        download list is allowed to shrink, and not below a few rows.
        """
        self.root.update_idletasks()
        fixed_height = self.top_frame.winfo_reqheight() + self.actions_bar.winfo_reqheight()
        min_width = max(MIN_WINDOW_WIDTH, self.root.winfo_reqwidth())
        min_height = max(MIN_WINDOW_HEIGHT, fixed_height + MIN_JOB_TABLE_HEIGHT)
        self.root.minsize(min_width, min_height)
        # The window would otherwise open at its natural height, which on a
        # 768-pixel screen puts Start downloads below the taskbar. Only the
        # download list gives up the difference.
        height = fit_height_to_screen(self.root.winfo_reqheight(), self.root.winfo_screenheight(), min_height)
        if height is not None:
            x = max(0, (self.root.winfo_screenwidth() - min_width) // 2)
            self.root.geometry(f"{min_width}x{height}+{x}+0")

    def _build_users_panel(self, parent: ttk.Frame) -> None:
        self.names_box = ttk.LabelFrame(parent, text="Usernames (one per line)", padding=8)
        box = self.names_box
        box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        box.rowconfigure(1, weight=1)
        box.columnconfigure(0, weight=1)

        chooser = ttk.Frame(box)
        chooser.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Radiobutton(
            chooser,
            text="Users",
            value="user",
            variable=self.var_source_type,
            command=self._on_source_changed,
        ).grid(row=0, column=0, padx=(0, 12))
        ttk.Radiobutton(
            chooser,
            text="Subreddits",
            value="subreddit",
            variable=self.var_source_type,
            command=self._on_source_changed,
        ).grid(row=0, column=1)

        self.text_usernames = tk.Text(box, height=12, width=30, undo=True)
        self.text_usernames.grid(row=1, column=0, sticky="nsew")
        self.text_usernames.insert("1.0", self.settings.usernames.strip())

        scroll = ttk.Scrollbar(box, orient="vertical", command=self.text_usernames.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.text_usernames.configure(yscrollcommand=scroll.set)

        hint = ttk.Label(box, text="Each name becomes its own download job.", foreground="grey")
        hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _on_source_changed(self) -> None:
        """Relabel the name box and grey out options that do not apply."""
        is_subreddit = self.var_source_type.get() == "subreddit"
        self.names_box.configure(text="Subreddits (one per line)" if is_subreddit else "Usernames (one per line)")
        state = "disabled" if is_subreddit else "normal"
        for widget in self._user_only_widgets:
            widget.configure(state=state)

    def _build_options_panel(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="Options", padding=8)
        box.grid(row=0, column=1, sticky="nsew")
        box.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(box, text="Output folder").grid(row=row, column=0, sticky="w", pady=2)
        folder_frame = ttk.Frame(box)
        folder_frame.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
        folder_frame.columnconfigure(0, weight=1)
        ttk.Entry(folder_frame, textvariable=self.var_directory).grid(row=0, column=0, sticky="ew")
        ttk.Button(folder_frame, text="Browse...", command=self.choose_directory).grid(row=0, column=1, padx=(6, 0))
        row += 1

        ttk.Label(box, text="Sort").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Combobox(
            box,
            textvariable=self.var_sort,
            values=SORT_CHOICES,
            state="readonly",
            width=16,
        ).grid(row=row, column=1, sticky="w", pady=2)
        row += 1

        sources = ttk.Frame(box)
        sources.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 2))
        self._user_only_widgets = []
        for column, (text, variable) in enumerate(
            (("Submitted", self.var_submitted), ("Saved", self.var_saved), ("Upvoted", self.var_upvoted))
        ):
            check = ttk.Checkbutton(sources, text=text, variable=variable)
            check.grid(row=0, column=column, padx=(0, 10))
            self._user_only_widgets.append(check)
        ttk.Checkbutton(sources, text="Authenticate", variable=self.var_authenticate).grid(row=0, column=3)
        row += 1

        # One row for both: another row would push Start downloads off a 768-pixel screen.
        file_options = ttk.Frame(box)
        file_options.grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Checkbutton(file_options, text="Skip duplicate files (--no-dupes)", variable=self.var_no_dupes).grid(
            row=0, column=0, padx=(0, 10)
        )
        # The option name is shown because the log's "already downloaded" summary refers to it.
        self.check_recheck = ttk.Checkbutton(
            file_options, text="Re-check posts already downloaded (--recheck)", variable=self.var_recheck
        )
        self.check_recheck.grid(row=0, column=1)
        row += 1

        for label, variable in (
            ("Skip extensions", self.var_skip),
            ("Folder scheme", self.var_folder_scheme),
            ("File scheme", self.var_file_scheme),
            ("Limit per user (blank = all)", self.var_limit),
        ):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(box, textvariable=variable).grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
            row += 1

        ttk.Separator(box, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
        row += 1

        ttk.Label(box, text="Simultaneous downloads").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Spinbox(box, from_=1, to=16, textvariable=self.var_max_parallel, width=6).grid(
            row=row, column=1, sticky="w", pady=2
        )
        row += 1

        ttk.Label(box, text="Connections per download").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Spinbox(box, from_=1, to=32, textvariable=self.var_concurrency, width=6).grid(
            row=row, column=1, sticky="w", pady=2
        )
        row += 1

        self._build_reddit_api_section(box, row)

    def _build_reddit_api_section(self, parent: ttk.LabelFrame, row: int) -> None:
        """Credentials for the user's own Reddit app.

        BDFR's bundled client ID shares one rate limit between every BDFR user,
        so a registered app of their own is what keeps a user's downloads from
        stalling on other people's traffic.
        """
        api = ttk.LabelFrame(parent, text="Reddit API (optional)", padding=6)
        api.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        api.columnconfigure(1, weight=1)
        fields = (
            ("Client ID", self.var_client_id, ""),
            ("Client secret", self.var_client_secret, "*"),
            ("Your Reddit username (for User-Agent)", self.var_reddit_username, ""),
        )
        self.api_entries: dict[str, ttk.Entry] = {}
        for api_row, (label, variable, mask) in enumerate(fields):
            ttk.Label(api, text=label).grid(row=api_row, column=0, sticky="w", padx=(0, 6), pady=2)
            entry = ttk.Entry(api, textvariable=variable, show=mask)
            entry.grid(row=api_row, column=1, sticky="ew", pady=2)
            self.api_entries[label] = entry
        ttk.Label(
            api,
            text=(
                "Blank = the app in your config file (BDFR's shared app by default).\n"
                f"New app: reddit.com/prefs/apps, redirect URI {OAUTH_REDIRECT_URI}."
            ),
            foreground="grey",
        ).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _build_actions(self, root: tk.Tk) -> None:
        bar = self.actions_bar = ttk.Frame(root, padding=(10, 0, 10, 6))
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(3, weight=1)

        self.button_start = ttk.Button(bar, text="Start downloads", command=self.start)
        self.button_start.grid(row=0, column=0)
        self.button_cancel = ttk.Button(bar, text="Cancel selected", command=self.cancel_selected, state="disabled")
        self.button_cancel.grid(row=0, column=1, padx=6)
        self.button_cancel_all = ttk.Button(bar, text="Cancel all", command=self.cancel_all, state="disabled")
        self.button_cancel_all.grid(row=0, column=2)
        ttk.Label(bar, textvariable=self.var_status, anchor="e").grid(row=0, column=3, sticky="ew", padx=(10, 0))

    def _build_job_table(self, root: tk.Tk) -> None:
        box = ttk.LabelFrame(root, text="Downloads", padding=8)
        box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 4))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        columns = ("user", "state", "done", "message")
        self.tree = ttk.Treeview(box, columns=columns, show="headings", selectmode="extended")
        for key, title, width, stretch in (
            ("user", "User", 160, False),
            ("state", "State", 100, False),
            ("done", "Done", 70, False),
            ("message", "Latest activity", 480, True),
        ):
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, stretch=stretch, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

    # -------------------------------------------------------------- actions

    def choose_directory(self) -> None:
        current = self.var_directory.get().strip()
        options = {"title": "Choose the download folder", "mustexist": False}
        if current and Path(current).is_dir():
            options["initialdir"] = current
        chosen = filedialog.askdirectory(**options)
        if chosen:
            self.var_directory.set(str(Path(chosen)))

    def start(self) -> None:
        settings = self.collect_settings()
        problem = validate_settings(settings)
        if problem:
            messagebox.showwarning("Cannot start", problem, parent=self.root)
            return

        self.settings = settings
        settings.save()

        target = Path(settings.directory)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Cannot start", f"Could not create {target}:\n{e}", parent=self.root)
            return

        if self.manager is not None:
            self.manager.shutdown()
        self.manager = JobManager(max_parallel=settings.max_parallel_jobs)
        self._row_for_job.clear()
        self.tree.delete(*self.tree.get_children())

        for username, config in build_job_configs(settings):
            job = self.manager.add_job(username, config)
            row = self.tree.insert("", "end", values=(username, job.state.value, 0, "Queued"))
            self._row_for_job[job.job_id] = row

        self.button_cancel.configure(state="normal")
        self.button_cancel_all.configure(state="normal")
        self.var_status.set(f"Queued {len(self._row_for_job)} downloads")

    def cancel_selected(self) -> None:
        if self.manager is None:
            return
        selected = set(self.tree.selection())
        for job_id, row in self._row_for_job.items():
            if row in selected:
                self.manager.cancel(job_id)
        self.refresh_table()

    def cancel_all(self) -> None:
        if self.manager is None:
            return
        self.manager.cancel_all()
        self.refresh_table()

    def on_close(self) -> None:
        if self.manager is not None and self.manager.running_count:
            if not messagebox.askokcancel(
                "Downloads running",
                f"{self.manager.running_count} download(s) are still running. Stop them and quit?",
                parent=self.root,
            ):
                return
        try:
            self.collect_settings().save()
        except tk.TclError:
            logger.debug("Window was already gone when saving settings")
        if self.manager is not None:
            self.manager.shutdown()
        self.root.destroy()

    # ------------------------------------------------------------- updating

    def _schedule_poll(self) -> None:
        self.root.after(POLL_INTERVAL_MS, self._poll)

    def _poll(self) -> None:
        try:
            if self.manager is not None:
                self.manager.pump()
                self.refresh_table()
                self._refresh_status()
        except Exception:
            logger.exception("Failed to refresh the download list")
        self._schedule_poll()

    def refresh_table(self) -> None:
        if self.manager is None:
            return
        for job in self.manager.ordered_jobs:
            row = self._row_for_job.get(job.job_id)
            if row is None or not self.tree.exists(row):
                continue
            self.tree.item(row, values=(job.label, job.state.value, job.completed, job.last_message))

    def _refresh_status(self) -> None:
        if self.manager is None:
            return
        jobs = self.manager.ordered_jobs
        if not jobs:
            return
        done = sum(1 for job in jobs if job.state.is_terminal)
        failed = sum(1 for job in jobs if job.state is JobState.FAILED)
        files = sum(job.completed for job in jobs)
        if self.manager.is_finished:
            self.var_status.set(f"Finished: {done}/{len(jobs)} jobs, {files} submissions, {failed} failed")
            self.button_cancel.configure(state="disabled")
            self.button_cancel_all.configure(state="disabled")
        else:
            self.var_status.set(
                f"Running {self.manager.running_count} of {len(jobs)} jobs, {files} submissions downloaded"
            )


def main() -> None:
    """Entry point for the `bdfr-gui` command."""
    import multiprocessing

    # Needed so that spawned child processes behave correctly if this is ever
    # packaged into a frozen executable.
    multiprocessing.freeze_support()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
    root = tk.Tk()
    try:
        # Prefer the native Windows theme when it is available.
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        logger.debug("Could not apply a native theme")
    BdfrGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
