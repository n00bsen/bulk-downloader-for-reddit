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
import re
import tkinter as tk
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TypeVar

import appdirs

from bdfr.configuration import Configuration
from bdfr.constants import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_PARALLEL_JOBS,
    MAX_CONCURRENCY,
    MAX_PARALLEL_JOBS,
    OAUTH_REDIRECT_URI,
)
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
# What enters a number typed into a spinbox: the Enter keys, and leaving the
# box. Not until then is it passed on to the downloads that are running. While
# a number is being typed the box holds other numbers for a moment ("35" on the
# way from 3 to 5), and a number that was too high cannot be taken back:
# lowering it again does not stop the jobs and transfers it has started.
NUMBER_ENTERED = "<<NumberEntered>>"
NUMBER_ENTERED_BY = ("<Return>", "<KP_Enter>", "<FocusOut>")
# What a step of a spinbox arrives as, from a click on one of its arrows and
# from the Up and Down keys.
STEP_UP = "<<Increment>>"
STEP_DOWN = "<<Decrement>>"
# What a turn of the mouse wheel arrives as; the two buttons are the wheel on X11.
MOUSE_WHEEL = ("<MouseWheel>", "<Button-4>", "<Button-5>")
# A decimal number as it is written by hand: digits, with or without a sign, a
# fraction and an exponent.
DECIMAL_NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
# Entries of the right-click menu of the text inputs. A menu entry is addressed
# by its label, so each label is written down once.
MENU_CUT = "Cut"
MENU_COPY = "Copy"
MENU_PASTE = "Paste"
MENU_SELECT_ALL = "Select all"

# A widget that text is typed into: the name box, an entry, or a spinbox, which is an entry too.
TextInput = tk.Text | ttk.Entry
TextInputT = TypeVar("TextInputT", bound=TextInput)


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


def first_line(text: str) -> str:
    """Return the part of pasted text that belongs in a one-line field.

    Text copied from a web page often carries a line break at either end, which
    would sit unseen in a field such as the client secret. As in a Windows text
    box, a one-line field takes the first line and drops the rest.
    """
    lines = text.strip("\r\n").splitlines()
    return lines[0] if lines else ""


def number_in_range(value: object, highest: int) -> int | None:
    """Return `value` as a whole number from 1 to `highest`, or None if it is not a number.

    A number outside the range is brought into it rather than refused: a
    spinbox keeps only its arrows within its range, not what is typed or
    pasted into it.
    """
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(1, min(highest, number))


def number_in_text(text: str) -> int | float | None:
    """Return the number that `text` spells out, or None if it spells none.

    The text is read as a decimal number, which is how somebody typing a number
    of downloads means it. Tcl, through which a spinbox's own variable is read,
    takes a leading zero for octal: "010" for 8, and "08" for no number at all.
    A number too large to count with comes back as infinity.
    """
    text = text.strip()
    if not DECIMAL_NUMBER.fullmatch(text):
        return None
    try:
        return int(text)
    except ValueError:
        # A fraction, an exponent, or more digits than Python converts in one go.
        return float(text)


@contextmanager
def single_undo_step(text: tk.Text) -> Iterator[None]:
    """Make the edits inside one step for Undo, the way Tk's own paste does.

    Left to itself the widget begins a new step between a delete and an insert,
    so undoing a paste over a selection would bring back only half of it.
    """
    automatic = text.cget("autoseparators")
    text.configure(autoseparators=False)
    text.edit_separator()
    try:
        yield
    finally:
        text.edit_separator()
        text.configure(autoseparators=automatic)


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


@dataclass(eq=False)
class NumberBox:
    """One of the two spinboxes whose number the downloads follow while they run."""

    spinbox: ttk.Spinbox
    variable: tk.IntVar
    # The number goes from 1 to this.
    highest: int
    # The number entered last. It is the one the downloads have, and it stands
    # in whenever the box itself holds none.
    entered: int
    # Tells the downloads that are running a number that has just been entered.
    pass_on: Callable[[int], None]


class BdfrGui:
    """The main window."""

    def __init__(self, root: tk.Tk, settings: GuiSettings | None = None):
        self.root = root
        self.settings = settings or GuiSettings.load()
        self.manager: JobManager | None = None
        self._row_for_job: dict[str, str] = {}

        root.title("BDFR Downloader")
        root.event_add(NUMBER_ENTERED, *NUMBER_ENTERED_BY)
        self._build_variables()
        self._build_context_menu()
        self._build_layout()
        root.bind("<Button-1>", self._leave_number_box, add="+")
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
        # A settings file edited by hand can hold anything, so the two numbers
        # are checked before they are shown.
        self.var_concurrency = tk.IntVar(value=number_in_range(s.concurrency, MAX_CONCURRENCY) or DEFAULT_CONCURRENCY)
        self.var_max_parallel = tk.IntVar(
            value=number_in_range(s.max_parallel_jobs, MAX_PARALLEL_JOBS) or DEFAULT_MAX_PARALLEL_JOBS
        )
        self.var_client_id = tk.StringVar(value=s.client_id)
        self.var_client_secret = tk.StringVar(value=s.client_secret)
        self.var_reddit_username = tk.StringVar(value=s.reddit_username)
        self.var_status = tk.StringVar(value="Ready")

    def _number_box(
        self, parent: ttk.LabelFrame, variable: tk.IntVar, highest: int, pass_on: Callable[[int], None]
    ) -> NumberBox:
        """Create a spinbox for a number from 1 to `highest`, which reaches the running downloads through `pass_on`.

        A step counts at once: a click on one of the arrows, or the Up or Down
        key, changes the number by one within the range. What is typed or
        pasted counts when it is entered and not at each keystroke: see
        NUMBER_ENTERED. The mouse wheel does nothing here. Tk would make a step
        of every notch, also while the pointer merely rests on a box that is
        not in use, and a number that went up cannot be taken back.
        """
        spinbox = self._with_context_menu(ttk.Spinbox(parent, from_=1, to=highest, textvariable=variable, width=6))
        box = NumberBox(spinbox, variable, highest, entered=variable.get(), pass_on=pass_on)
        spinbox.bind(NUMBER_ENTERED, lambda _event: self._number_entered(box))
        spinbox.bind(STEP_UP, lambda _event: self._step_number(box, +1))
        spinbox.bind(STEP_DOWN, lambda _event: self._step_number(box, -1))
        for wheel in MOUSE_WHEEL:
            spinbox.bind(wheel, lambda _event: "break")
        spinbox.bind("<<Paste>>", self._paste_from_keyboard)
        return box

    def _pass_on_max_parallel(self, number: int) -> None:
        if self.manager is not None:
            self.manager.set_max_parallel(number)

    def _pass_on_concurrency(self, number: int) -> None:
        if self.manager is not None:
            self.manager.set_concurrency(number)

    @staticmethod
    def _number_in(box: NumberBox) -> int:
        """Return the number a box stands for: its own, brought into the range, or the one entered last.

        The one entered last stands in while the box holds no number: it is
        empty or half typed while it is being edited, and it can hold text that
        reads as infinity, such as "1e999".
        """
        number = number_in_range(number_in_text(box.spinbox.get()), box.highest)
        return box.entered if number is None else number

    def _enter_number(self, box: NumberBox, number: int | None = None) -> None:
        """Make `number` the one that counts, or the number the box stands for when none is given.

        The box then shows exactly that number: one outside the range has
        given way to the nearest one inside it, and text that is no number to
        the number entered last. The downloads that are running are told if
        the number is a new one.
        """
        if number is None:
            number = self._number_in(box)
        if box.spinbox.get() != str(number):
            box.variable.set(number)
        if number != box.entered:
            box.entered = number
            box.pass_on(number)

    def _number_entered(self, box: NumberBox) -> None:
        """Enter what a box holds, on one of the events of NUMBER_ENTERED."""
        if not self._menu_has_keyboard_of(box.spinbox):
            self._enter_number(box)

    def _step_number(self, box: NumberBox, step: int) -> str:
        """Count a box's number up or down by one, within the range, and enter the result at once.

        This takes the place of Tk's own step, which starts from whatever text
        the box holds. With a 5 typed behind a 3 and not entered yet, the down
        arrow would make 16 of that "35", and a number that went up cannot be
        taken back. A step starts from the number in the box only if that is
        inside the range, and from the number entered last otherwise.
        """
        typed = number_in_text(box.spinbox.get())
        start = int(typed) if typed is not None and 1 <= typed <= box.highest else box.entered
        self._enter_number(box, max(1, min(box.highest, start + step)))
        # As Tk's own step leaves the box: all of it selected, for typing to replace.
        box.spinbox.selection_range(0, tk.END)
        box.spinbox.icursor(tk.END)
        return "break"

    def _leave_number_box(self, event: tk.Event) -> None:
        """Enter the number being typed when a click lands anywhere outside its box.

        A click on a label, a frame or the window itself moves the keyboard
        focus nowhere. The box would not learn that it has been left, and its
        number would stay unentered with nothing to show for it. This runs
        after the clicked widget's own bindings, so a widget that takes the
        focus has it by now and leaves nothing to do here.
        """
        # "-lastfor" still answers while another application is in front.
        in_use = str(self.root.tk.call("focus", "-lastfor", self.root))
        for box in self._number_boxes:
            if in_use == str(box.spinbox) and str(event.widget) != in_use:
                self.root.focus_set()
                # Losing the focus enters the number too, but only in a window that has the keyboard.
                self._enter_number(box)

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
            # Neither a start nor saving on the way out may fail on a spinbox that holds no number.
            concurrency=self._number_in(self.number_concurrency),
            max_parallel_jobs=self._number_in(self.number_max_parallel),
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

        self.text_usernames = self._with_context_menu(tk.Text(box, height=12, width=30, undo=True))
        self.text_usernames.grid(row=1, column=0, sticky="nsew")
        self.text_usernames.insert("1.0", self.settings.usernames.strip())

        scroll = ttk.Scrollbar(box, orient="vertical", command=self.text_usernames.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.text_usernames.configure(yscrollcommand=scroll.set)

        # The button shares the hint's row: this box is shorter than the options
        # beside it, so a taller row here does not make the window any taller.
        footer = ttk.Frame(box)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        footer.columnconfigure(0, weight=1)
        hint = ttk.Label(footer, text="Each name becomes its own download job.", foreground="grey")
        hint.grid(row=0, column=0, sticky="w")
        self.button_paste = ttk.Button(footer, text="Paste", width=6, command=self.paste_usernames)
        self.button_paste.grid(row=0, column=1, sticky="e", padx=(6, 0))

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
        self.entry_directory = self._with_context_menu(ttk.Entry(folder_frame, textvariable=self.var_directory))
        self.entry_directory.grid(row=0, column=0, sticky="ew")
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

        self.option_entries: dict[str, ttk.Entry] = {}
        for label, variable in (
            ("Skip extensions", self.var_skip),
            ("Folder scheme", self.var_folder_scheme),
            ("File scheme", self.var_file_scheme),
            ("Limit per user (blank = all)", self.var_limit),
        ):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", pady=2)
            entry = self._with_context_menu(ttk.Entry(box, textvariable=variable))
            entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
            self.option_entries[label] = entry
            row += 1

        ttk.Separator(box, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
        row += 1

        ttk.Label(box, text="Simultaneous downloads").grid(row=row, column=0, sticky="w", pady=2)
        self.number_max_parallel = self._number_box(
            box, self.var_max_parallel, MAX_PARALLEL_JOBS, self._pass_on_max_parallel
        )
        self.spin_max_parallel = self.number_max_parallel.spinbox
        self.spin_max_parallel.grid(row=row, column=1, sticky="w", pady=2)
        row += 1

        ttk.Label(box, text="Connections per download").grid(row=row, column=0, sticky="w", pady=2)
        self.number_concurrency = self._number_box(
            box, self.var_concurrency, MAX_CONCURRENCY, self._pass_on_concurrency
        )
        self.spin_concurrency = self.number_concurrency.spinbox
        self.spin_concurrency.grid(row=row, column=1, sticky="w", pady=2)
        row += 1
        self._number_boxes = (self.number_max_parallel, self.number_concurrency)

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
            entry = self._with_context_menu(ttk.Entry(api, textvariable=variable, show=mask))
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

    # ------------------------------------------------------------ clipboard

    def _read_clipboard(self) -> str:
        """Return the text on the clipboard, or "" when it is empty or holds something else."""
        try:
            return self.root.clipboard_get()
        except tk.TclError:
            return ""

    def _write_clipboard(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError:
            logger.debug("Could not put text on the clipboard")

    def _build_context_menu(self) -> None:
        """Create the right-click menu that every text input shares.

        Tk gives its inputs keyboard shortcuts only, so without this a paste
        needs Ctrl+V.
        """
        menu = self.context_menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=MENU_CUT, command=self._menu_cut)
        menu.add_command(label=MENU_COPY, command=self._menu_copy)
        menu.add_command(label=MENU_PASTE, command=self._menu_paste)
        menu.add_separator()
        menu.add_command(label=MENU_SELECT_ALL, command=self._menu_select_all)
        # The input the menu was last opened on, which its commands act on.
        self._menu_target: TextInput | None = None
        windowing_system = self.root.tk.call("tk", "windowingsystem")
        self._menu_events = ["<Button-3>"]
        if windowing_system == "aqua":
            # A secondary click on a Mac arrives as one of these instead.
            self._menu_events += ["<Button-2>", "<Control-Button-1>"]
        # On X11 the menu is still up when tk_popup returns; elsewhere it is gone by then.
        self._menu_outlasts_popup = windowing_system == "x11"

    def _with_context_menu(self, widget: TextInputT) -> TextInputT:
        """Give a text input the right-click menu, and return it."""
        for event in self._menu_events:
            widget.bind(event, self._show_context_menu)
        return widget

    def _show_context_menu(self, event: tk.Event) -> str:
        widget = event.widget
        # Asked before the click moves the focus here. "-lastfor" still answers
        # while another application is in front.
        was_in_use = str(self.root.tk.call("focus", "-lastfor", widget)) == str(widget)
        widget.focus_set()
        clicked = self._index_at(widget, event.x, event.y)
        if not (was_in_use and self._selection_contains(widget, clicked)):
            # Paste then lands where the click was, as it would after a left
            # click. Only a click on the selection of the field in use leaves
            # that selection alone, to be cut, copied or pasted over. Any other
            # selection is not what the click was aimed at, and one left behind
            # in a field that has lost the focus is not even shown on Windows:
            # pasting over it would replace text nobody pointed at.
            self._clear_selection(widget)
            self._put_cursor(widget, clicked)
        self._menu_target = widget
        # A masked field hides its text; copying it out would undo that.
        can_copy = "normal" if self._has_selection(widget) and not self._is_masked(widget) else "disabled"
        self.context_menu.entryconfigure(MENU_CUT, state=can_copy)
        self.context_menu.entryconfigure(MENU_COPY, state=can_copy)
        self.context_menu.entryconfigure(MENU_PASTE, state="normal" if self._text_to_paste(widget) else "disabled")
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            # The menu is gone, with or without a choice, and nothing of it may
            # go on holding the pointer: the window would ignore the next click.
            # Not on X11, where the menu is still up at this point and its grab
            # is what lets a click elsewhere close it; Tk gives that grab up by
            # itself when the menu goes.
            if not self._menu_outlasts_popup:
                self.context_menu.grab_release()
        return "break"

    def _keyboard_widget(self) -> str:
        """Return the Tk name of the widget that has the keyboard focus, "" while this application has not got it."""
        return str(self.root.tk.call("focus"))

    def _menu_has_keyboard_of(self, widget: TextInput) -> bool:
        """Tell whether the right-click menu is up on `widget` and has taken the keyboard focus from it.

        That is how Tk shows the menu on X11. The field under the menu is told
        that it has lost the focus, but it has not been left: the focus comes
        back to it when the menu goes, before a choice from the menu is carried
        out. A menu opened on another field is different. The click that
        opened it did take the focus away from here.
        """
        return self._menu_target is widget and self._keyboard_widget() == str(self.context_menu)

    def _text_to_paste(self, widget: TextInput) -> str:
        """Return the clipboard's text as a paste would put it into `widget`, "" when it has nothing for it."""
        text = self._read_clipboard()
        if isinstance(widget, tk.Text):
            return text
        text = first_line(text)
        if isinstance(widget, ttk.Spinbox):
            # A number box takes a whole number or nothing. Anything else would
            # sit in it as text that neither a start nor the running downloads
            # can use.
            text = text.strip()
            return text if text.isascii() and text.isdigit() else ""
        return text

    @staticmethod
    def _is_masked(widget: TextInput) -> bool:
        return not isinstance(widget, tk.Text) and bool(str(widget.cget("show")))

    @staticmethod
    def _has_selection(widget: TextInput) -> bool:
        if isinstance(widget, tk.Text):
            return bool(widget.tag_ranges(tk.SEL))
        return bool(widget.selection_present())

    @staticmethod
    def _selected_text(widget: TextInput) -> str:
        if isinstance(widget, tk.Text):
            return widget.get(tk.SEL_FIRST, tk.SEL_LAST)
        return widget.get()[widget.index(tk.SEL_FIRST) : widget.index(tk.SEL_LAST)]

    def _selection_contains(self, widget: TextInput, index: str | int) -> bool:
        """Tell whether `index`, as `_index_at` returns it, lies on the selection, either end included."""
        if not self._has_selection(widget):
            return False
        if isinstance(widget, tk.Text):
            return widget.compare(tk.SEL_FIRST, "<=", index) and widget.compare(index, "<=", tk.SEL_LAST)
        return widget.index(tk.SEL_FIRST) <= index <= widget.index(tk.SEL_LAST)

    @staticmethod
    def _clear_selection(widget: TextInput) -> None:
        if isinstance(widget, tk.Text):
            widget.tag_remove(tk.SEL, "1.0", tk.END)
        else:
            widget.selection_clear()

    @staticmethod
    def _index_at(widget: TextInput, x: int, y: int) -> str | int:
        """Return the gap between characters nearest to a click, where the insertion cursor would go.

        "@x" names the character under the pointer; a click on its right half
        belongs to the gap after it. The end of a line is the exception: its
        box reaches to the edge of the widget, and a click anywhere in it means
        the end of that line, not the start of the next.
        """
        if isinstance(widget, tk.Text):
            index = widget.index(f"@{x},{y}")
            box = widget.bbox(index)
            if box and widget.get(index) != "\n" and x - box[0] >= box[2] / 2:
                index = widget.index(f"{index} + 1 char")
            return index
        index = widget.index(f"@{x}")
        box = widget.bbox(index)
        if box and x - box[0] > box[2] / 2:
            # Not past the end of the text, where a click in the empty part of the field would otherwise land.
            index = min(index + 1, widget.index(tk.END))
        return index

    @staticmethod
    def _put_cursor(widget: TextInput, index: str | int) -> None:
        if isinstance(widget, tk.Text):
            widget.mark_set(tk.INSERT, index)
        else:
            widget.icursor(index)

    def _replace_selection(self, widget: TextInput, text: str) -> None:
        """Put `text` in place of the selection, or at the cursor when nothing is selected."""
        selected = self._has_selection(widget)
        if isinstance(widget, tk.Text):
            with single_undo_step(widget):
                if selected:
                    widget.mark_set(tk.INSERT, tk.SEL_FIRST)
                    widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
                widget.insert(tk.INSERT, text)
            widget.see(tk.INSERT)
            return
        if selected:
            widget.icursor(tk.SEL_FIRST)
            widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
        widget.insert(tk.INSERT, text)
        # Keep the cursor in view when the text is longer than the field, as typing does.
        cursor = widget.index(tk.INSERT)
        if cursor < widget.index("@0") or cursor >= widget.index(f"@{widget.winfo_width()}"):
            widget.xview(cursor)

    def _menu_cut(self) -> None:
        widget = self._menu_target
        if widget is None or not self._has_selection(widget) or self._is_masked(widget):
            return
        self._write_clipboard(self._selected_text(widget))
        self._replace_selection(widget, "")

    def _menu_copy(self) -> None:
        widget = self._menu_target
        if widget is None or not self._has_selection(widget) or self._is_masked(widget):
            return
        self._write_clipboard(self._selected_text(widget))

    def _paste_into(self, widget: TextInput) -> None:
        text = self._text_to_paste(widget)
        if text:
            self._replace_selection(widget, text)

    def _menu_paste(self) -> None:
        if self._menu_target is not None:
            self._paste_into(self._menu_target)

    def _paste_from_keyboard(self, event: tk.Event) -> str:
        """Paste on Ctrl+V or Shift+Insert as the menu's Paste does, in place of Tk's own paste.

        The number boxes are bound to this: Tk's paste would put any text into
        them, and they take a whole number or nothing.
        """
        self._paste_into(event.widget)
        return "break"

    def _menu_select_all(self) -> None:
        widget = self._menu_target
        if isinstance(widget, tk.Text):
            # Up to, not including, the line break Tk keeps after the last line.
            widget.tag_add(tk.SEL, "1.0", "end-1c")
            widget.mark_set(tk.INSERT, "end-1c")
        elif widget is not None:
            widget.selection_range(0, tk.END)
            widget.icursor(tk.END)

    # -------------------------------------------------------------- actions

    def paste_usernames(self) -> None:
        """Add the clipboard's text to the end of the name box, starting on a new line.

        This is the one-click way to collect names: copy a name or a list
        somewhere else, press the button, repeat.
        """
        pasted = self._read_clipboard().strip()
        if not pasted:
            return
        box = self.text_usernames
        existing = box.get("1.0", "end-1c")
        with single_undo_step(box):
            # Only when the last line holds a name: a box that already ends
            # with a line break would otherwise get an empty line in between.
            if existing and not existing.endswith("\n"):
                box.insert(tk.END, "\n")
            box.insert(tk.END, pasted)
        box.mark_set(tk.INSERT, tk.END)
        box.see(tk.END)

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
        # The two spinboxes steer the downloads from here on, so the numbers
        # these start with count as entered and the boxes show exactly them.
        for box in self._number_boxes:
            self._enter_number(box)

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
