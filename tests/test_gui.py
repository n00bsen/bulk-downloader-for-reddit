#!/usr/bin/env python3

"""Tests for the GUI layer.

Only the pure logic is covered here -- settings round-tripping, input parsing
and the translation from window state into per-job Configuration objects. That
is where the behaviour that used to live in the batch file now lives, so it is
the part worth pinning down. Widget construction needs a display and is checked
separately by the `gui` marked test.
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bdfr.configuration import Configuration
from bdfr.constants import DEFAULT_CONCURRENCY, DEFAULT_MAX_PARALLEL_JOBS, MAX_CONCURRENCY, MAX_PARALLEL_JOBS
from bdfr.gui import (
    MENU_COPY,
    MENU_CUT,
    MENU_PASTE,
    MENU_SELECT_ALL,
    MIN_JOB_TABLE_HEIGHT,
    MOUSE_WHEEL,
    NUMBER_ENTERED,
    NUMBER_ENTERED_BY,
    SCREEN_HEIGHT_ALLOWANCE,
    STEP_DOWN,
    STEP_UP,
    GuiSettings,
    build_job_configs,
    first_line,
    fit_height_to_screen,
    number_in_range,
    number_in_text,
    parse_skip_list,
    parse_usernames,
    validate_settings,
)
from bdfr.jobs import JobManager


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("alice", ["alice"]),
        ("alice\nbob", ["alice", "bob"]),
        ("alice, bob", ["alice", "bob"]),
        ("alice; bob", ["alice", "bob"]),
        ("  alice  \n\n  bob  \n", ["alice", "bob"]),
        ("u/alice\nU/bob", ["alice", "bob"]),
        ("/alice/", ["alice"]),
        ("alice\nalice\nbob", ["alice", "bob"]),
        ("", []),
        ("\n\n  \n", []),
        ("alice\nbob, carol; dave", ["alice", "bob", "carol", "dave"]),
    ),
)
def test_parse_usernames(raw: str, expected: list[str]):
    assert parse_usernames(raw) == expected


def test_parse_usernames_preserves_order():
    assert parse_usernames("zeta\nalpha\nmid") == ["zeta", "alpha", "mid"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("gif, avi", ["gif", "avi"]),
        (".gif,.avi", ["gif", "avi"]),
        ("gif; avi", ["gif", "avi"]),
        ("  gif  ,  avi  ", ["gif", "avi"]),
        ("", []),
        (",,", []),
    ),
)
def test_parse_skip_list(raw: str, expected: list[str]):
    assert parse_skip_list(raw) == expected


def test_defaults_match_the_batch_file_they_replace():
    """The first launch must behave like the old launcher."""
    settings = GuiSettings()
    assert settings.directory == "D:\\reddit"
    assert settings.sort == "new"
    assert settings.submitted is True
    assert settings.no_dupes is True
    assert parse_skip_list(settings.skip) == ["gif", "avi"]
    assert settings.folder_scheme == "{REDDITOR}"


def test_settings_round_trip(tmp_path: Path):
    path = tmp_path / "gui_settings.json"
    original = GuiSettings(
        directory=str(tmp_path / "out"),
        usernames="alice\nbob",
        sort="top",
        submitted=False,
        saved=True,
        authenticate=True,
        concurrency=9,
        max_parallel_jobs=5,
    )
    original.save(path)
    restored = GuiSettings.load(path)
    assert restored == original


def test_settings_load_missing_file_returns_defaults(tmp_path: Path):
    assert GuiSettings.load(tmp_path / "absent.json") == GuiSettings()


def test_settings_load_corrupt_file_returns_defaults(tmp_path: Path):
    path = tmp_path / "gui_settings.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert GuiSettings.load(path) == GuiSettings()


def test_settings_load_ignores_unknown_keys(tmp_path: Path):
    """An older or newer settings file must not crash the window."""
    path = tmp_path / "gui_settings.json"
    path.write_text(json.dumps({"directory": "E:\\x", "from_the_future": 1}), encoding="utf-8")
    restored = GuiSettings.load(path)
    assert restored.directory == "E:\\x"
    assert not hasattr(restored, "from_the_future")


def test_settings_save_is_atomic(tmp_path: Path):
    path = tmp_path / "gui_settings.json"
    GuiSettings().save(path)
    assert [p.name for p in tmp_path.iterdir()] == ["gui_settings.json"]


def test_build_job_configs_makes_one_job_per_user(tmp_path: Path):
    settings = GuiSettings(usernames="alice\nbob\ncarol", directory=str(tmp_path))
    jobs = build_job_configs(settings)
    assert [label for label, _ in jobs] == ["alice", "bob", "carol"]
    assert [config.user for _, config in jobs] == [["alice"], ["bob"], ["carol"]]


def test_build_job_configs_gives_each_job_its_own_configuration(tmp_path: Path):
    """Configs are sent to separate processes and must not be shared."""
    settings = GuiSettings(usernames="alice\nbob", directory=str(tmp_path))
    (_, first), (_, second) = build_job_configs(settings)
    assert first is not second
    first.user.append("mutated")
    assert second.user == ["bob"]


def test_build_job_configs_carries_every_option(tmp_path: Path):
    settings = GuiSettings(
        usernames="alice",
        directory=str(tmp_path),
        sort="top",
        submitted=True,
        saved=True,
        upvoted=True,
        authenticate=True,
        no_dupes=False,
        skip="gif, avi, mp4",
        folder_scheme="{SUBREDDIT}",
        file_scheme="{POSTID}",
        limit="25",
        concurrency=7,
    )
    ((_, config),) = build_job_configs(settings)
    assert config.directory == str(tmp_path)
    assert config.sort == "top"
    assert config.submitted is True
    assert config.saved is True
    assert config.upvoted is True
    assert config.authenticate is True
    assert config.no_dupes is False
    assert config.skip == ["gif", "avi", "mp4"]
    assert config.folder_scheme == "{SUBREDDIT}"
    assert config.file_scheme == "{POSTID}"
    assert config.limit == 25
    assert config.concurrency == 7


def test_recheck_is_off_by_default(tmp_path: Path):
    """Off is what lets a re-run skip posts that are already downloaded."""
    assert GuiSettings().recheck is False
    ((_, config),) = build_job_configs(GuiSettings(usernames="alice", directory=str(tmp_path)))
    assert config.recheck is False


@pytest.mark.parametrize("source_type", ("user", "subreddit"))
def test_build_job_configs_carries_recheck(source_type: str, tmp_path: Path):
    settings = GuiSettings(source_type=source_type, usernames="alice\nbob", directory=str(tmp_path), recheck=True)
    assert [config.recheck for _, config in build_job_configs(settings)] == [True, True]


def test_recheck_round_trips(tmp_path: Path):
    path = tmp_path / "gui_settings.json"
    GuiSettings(usernames="alice", recheck=True).save(path)
    assert GuiSettings.load(path).recheck is True


def test_settings_from_before_recheck_still_load(tmp_path: Path):
    path = tmp_path / "gui_settings.json"
    path.write_text(json.dumps({"directory": "E:\\x", "usernames": "alice", "no_dupes": False}), encoding="utf-8")
    restored = GuiSettings.load(path)
    assert restored.recheck is False
    assert restored.no_dupes is False


def test_build_job_configs_leaves_log_unset(tmp_path: Path):
    """An unset log is what gives each process its own log file."""
    ((_, config),) = build_job_configs(GuiSettings(usernames="alice", directory=str(tmp_path)))
    assert config.log is None


@pytest.mark.parametrize("raw_limit", ("", "   ", "not-a-number"))
def test_build_job_configs_tolerates_bad_limit(raw_limit: str, tmp_path: Path):
    ((_, config),) = build_job_configs(GuiSettings(usernames="alice", directory=str(tmp_path), limit=raw_limit))
    assert config.limit is None


def test_build_job_configs_with_no_users_is_empty():
    assert build_job_configs(GuiSettings(usernames="")) == []


def test_validate_accepts_a_usable_configuration(tmp_path: Path):
    assert validate_settings(GuiSettings(usernames="alice", directory=str(tmp_path))) is None


@pytest.mark.parametrize(
    ("settings", "expected_fragment"),
    (
        (GuiSettings(usernames=""), "at least one username"),
        (GuiSettings(usernames="alice", directory="  "), "output folder"),
        (GuiSettings(usernames="alice", submitted=False), "Submitted, Saved or Upvoted"),
        (GuiSettings(usernames="alice", saved=True, authenticate=False), "require Authenticate"),
        (GuiSettings(usernames="alice", upvoted=True, authenticate=False), "require Authenticate"),
    ),
)
def test_validate_rejects_unusable_configurations(settings: GuiSettings, expected_fragment: str):
    problem = validate_settings(settings)
    assert problem is not None
    assert expected_fragment in problem


def test_validate_allows_saved_when_authenticated():
    settings = GuiSettings(usernames="alice", submitted=False, saved=True, authenticate=True)
    assert validate_settings(settings) is None


def _new_tk_root():
    """Create a Tk root window, or skip the test when there is no usable display.

    On Windows, Tcl now and then fails to read its own init.tcl ("couldn't read
    file ... init.tcl: No error") while pytest's fd-level capture has replaced
    the standard handles. A second attempt succeeds, so only repeated failure
    means the GUI genuinely cannot run here.
    """
    tk = pytest.importorskip("tkinter")
    error = None
    for _ in range(3):
        try:
            return tk.Tk()
        except tk.TclError as e:
            error = e
    pytest.skip(f"No display available for the GUI: {error}")


@pytest.mark.gui
def test_window_builds_and_collects_settings(tmp_path: Path):
    """Smoke test for the widget layer; needs a display."""
    root = _new_tk_root()

    from bdfr.gui import BdfrGui

    try:
        gui = BdfrGui(root, GuiSettings(usernames="alice\nbob", directory=str(tmp_path)))
        root.update_idletasks()
        collected = gui.collect_settings()
        assert parse_usernames(collected.usernames) == ["alice", "bob"]
        assert collected.directory == str(tmp_path)
        assert gui.tree["columns"] == ("user", "state", "done", "message")
        # Cancel controls stay disabled until something is queued.
        assert str(gui.button_cancel["state"]) == "disabled"
    finally:
        root.destroy()


def test_subreddit_mode_sets_subreddit_not_user(tmp_path: Path):
    settings = GuiSettings(source_type="subreddit", usernames="aww\npics", directory=str(tmp_path))
    jobs = build_job_configs(settings)
    assert [label for label, _ in jobs] == ["aww", "pics"]
    for _, config in jobs:
        assert config.user == []
    assert [config.subreddit for _, config in jobs] == [["aww"], ["pics"]]


def test_subreddit_mode_does_not_set_redditor_listings(tmp_path: Path):
    """Submitted/Saved/Upvoted are redditor listings and must stay off."""
    settings = GuiSettings(
        source_type="subreddit",
        usernames="aww",
        directory=str(tmp_path),
        submitted=True,
        saved=True,
        upvoted=True,
    )
    ((_, config),) = build_job_configs(settings)
    assert config.submitted is False
    assert config.saved is False
    assert config.upvoted is False


def test_subreddit_mode_is_valid_without_listing_flags():
    settings = GuiSettings(source_type="subreddit", usernames="aww", submitted=False)
    assert validate_settings(settings) is None


def test_subreddit_mode_still_requires_names():
    problem = validate_settings(GuiSettings(source_type="subreddit", usernames=""))
    assert problem is not None
    assert "subreddit" in problem


def test_source_type_round_trips(tmp_path: Path):
    path = tmp_path / "gui_settings.json"
    GuiSettings(source_type="subreddit", usernames="aww").save(path)
    assert GuiSettings.load(path).source_type == "subreddit"


def test_default_source_type_is_user():
    assert GuiSettings().source_type == "user"


def test_reddit_api_fields_default_to_blank():
    """Blank means BDFR's bundled app, so an upgrade changes nothing for existing users."""
    settings = GuiSettings()
    assert settings.client_id == ""
    assert settings.client_secret == ""
    assert settings.reddit_username == ""


def test_reddit_api_fields_round_trip(tmp_path: Path):
    path = tmp_path / "gui_settings.json"
    original = GuiSettings(client_id="my-client-id", client_secret="my-secret", reddit_username="alice")
    original.save(path)
    restored = GuiSettings.load(path)
    assert restored == original
    assert (restored.client_id, restored.client_secret, restored.reddit_username) == (
        "my-client-id",
        "my-secret",
        "alice",
    )


def test_settings_repr_hides_client_secret(tmp_path: Path):
    settings = GuiSettings(client_id="my-client-id", client_secret="sentinel-secret-value")
    assert "sentinel-secret-value" not in repr(settings)
    assert "my-client-id" in repr(settings)
    # Hiding it from the repr must not stop it being saved.
    path = tmp_path / "gui_settings.json"
    settings.save(path)
    assert GuiSettings.load(path).client_secret == "sentinel-secret-value"


@pytest.mark.parametrize(
    ("wanted", "screen_height", "min_height", "expected"),
    (
        # The user's 1080-pixel screen: the natural height fits and is kept.
        (777, 1080, 662, None),
        # A 1366x768 laptop: shrink the download list so the action bar stays on screen.
        (777, 768, 662, 668),
        # Too small even for the minimum size: never go below it.
        (777, 700, 662, 662),
    ),
)
def test_fit_height_to_screen(wanted: int, screen_height: int, min_height: int, expected: int | None):
    assert fit_height_to_screen(wanted, screen_height, min_height) == expected


def test_settings_from_before_reddit_api_fields_still_load(tmp_path: Path):
    path = tmp_path / "gui_settings.json"
    path.write_text(json.dumps({"directory": "E:\\x", "usernames": "alice"}), encoding="utf-8")
    restored = GuiSettings.load(path)
    assert restored.client_id == ""
    assert restored.client_secret == ""


@pytest.mark.parametrize("source_type", ("user", "subreddit"))
def test_build_job_configs_carries_reddit_api_fields(source_type: str, tmp_path: Path):
    settings = GuiSettings(
        source_type=source_type,
        usernames="alice\nbob",
        directory=str(tmp_path),
        client_id="  my-client-id ",
        client_secret=" my-secret ",
        reddit_username=" carol ",
    )
    for _, config in build_job_configs(settings):
        assert config.client_id == "my-client-id"
        assert config.client_secret == "my-secret"
        assert config.reddit_username == "carol"
        assert config.user_agent is None


def test_build_job_configs_blank_reddit_api_fields_mean_bundled_app(tmp_path: Path):
    settings = GuiSettings(
        usernames="alice", directory=str(tmp_path), client_id=" ", client_secret="", reddit_username=""
    )
    ((_, config),) = build_job_configs(settings)
    assert config.client_id is None
    assert config.client_secret is None
    assert config.reddit_username is None


def test_build_job_configs_blank_secret_with_client_id_is_installed_app(tmp_path: Path):
    settings = GuiSettings(usernames="alice", directory=str(tmp_path), client_id="my-installed-app")
    ((_, config),) = build_job_configs(settings)
    assert config.client_id == "my-installed-app"
    assert config.client_secret is None


def test_build_job_configs_drops_secret_without_client_id(tmp_path: Path):
    """A secret alone would be paired with the bundled ID and could never authenticate."""
    settings = GuiSettings(usernames="alice", directory=str(tmp_path), client_secret="orphan-secret")
    ((_, config),) = build_job_configs(settings)
    assert config.client_id is None
    assert config.client_secret is None


def test_validate_rejects_secret_without_client_id():
    problem = validate_settings(GuiSettings(usernames="alice", client_secret="orphan-secret"))
    assert problem is not None
    assert "Client ID" in problem


def test_validate_accepts_client_id_without_secret():
    assert validate_settings(GuiSettings(usernames="alice", client_id="my-installed-app")) is None


@pytest.mark.gui
def test_reddit_api_section_masks_secret_and_fits_minimum_size(tmp_path: Path):
    root = _new_tk_root()

    from bdfr.gui import BdfrGui

    try:
        settings = GuiSettings(
            usernames="alice",
            directory=str(tmp_path),
            client_id="my-client-id",
            client_secret="my-secret",
            reddit_username="carol",
        )
        gui = BdfrGui(root, settings)
        root.update_idletasks()
        assert str(gui.api_entries["Client secret"].cget("show")) == "*"
        assert str(gui.api_entries["Client ID"].cget("show")) == ""

        collected = gui.collect_settings()
        assert (collected.client_id, collected.client_secret, collected.reddit_username) == (
            "my-client-id",
            "my-secret",
            "carol",
        )

        # At the minimum size every option must get the space it asks for;
        # only the download list may shrink, and not below a few rows.
        min_width, min_height = root.minsize()
        assert min_width >= root.winfo_reqwidth()
        assert min_width >= gui.top_frame.winfo_reqwidth()
        fixed_height = gui.top_frame.winfo_reqheight() + gui.actions_bar.winfo_reqheight()
        assert min_height >= fixed_height + MIN_JOB_TABLE_HEIGHT
    finally:
        root.destroy()


@pytest.mark.gui
def test_window_opens_within_a_short_screen(tmp_path: Path):
    """On a 768-pixel screen the natural height would put Start downloads under the taskbar."""
    root = _new_tk_root()

    from bdfr.gui import BdfrGui

    try:
        root.winfo_screenheight = lambda: 768
        BdfrGui(root, GuiSettings(usernames="alice", directory=str(tmp_path)))
        root.update_idletasks()
        _, min_height = root.minsize()
        assert root.winfo_reqheight() > 768 - SCREEN_HEIGHT_ALLOWANCE, "window no longer needs fitting"
        size, _, top = root.geometry().partition("+")
        height = int(size.split("x")[1])
        assert min_height <= height <= 768 - SCREEN_HEIGHT_ALLOWANCE
        assert top.endswith("+0")
    finally:
        root.destroy()


@pytest.mark.gui
def test_recheck_checkbox_round_trips_through_the_window(tmp_path: Path):
    root = _new_tk_root()

    from bdfr.gui import BdfrGui

    try:
        gui = BdfrGui(root, GuiSettings(usernames="alice", directory=str(tmp_path), recheck=True))
        root.update_idletasks()
        assert "Re-check posts already downloaded" in str(gui.check_recheck.cget("text"))
        assert gui.collect_settings().recheck is True

        gui.check_recheck.invoke()
        collected = gui.collect_settings()
        assert collected.recheck is False
        path = tmp_path / "gui_settings.json"
        collected.save(path)
        assert GuiSettings.load(path).recheck is False
    finally:
        root.destroy()


@pytest.mark.gui
def test_switching_to_subreddits_relabels_and_disables_listing_options(tmp_path: Path):
    root = _new_tk_root()

    from bdfr.gui import BdfrGui

    try:
        gui = BdfrGui(root, GuiSettings(usernames="alice", directory=str(tmp_path)))
        root.update_idletasks()
        assert "Usernames" in gui.names_box.cget("text")
        assert all(str(w["state"]) == "normal" for w in gui._user_only_widgets)

        gui.var_source_type.set("subreddit")
        gui._on_source_changed()
        root.update_idletasks()
        assert "Subreddits" in gui.names_box.cget("text")
        assert all(str(w["state"]) == "disabled" for w in gui._user_only_widgets)
        assert gui.collect_settings().source_type == "subreddit"
    finally:
        root.destroy()


@pytest.mark.parametrize(
    ("pasted", "expected"),
    (
        ("alice", "alice"),
        ("alice\n", "alice"),
        ("\r\nalice\r\n", "alice"),
        ("alice\nbob", "alice"),
        ("  kept as it is  ", "  kept as it is  "),
        ("", ""),
        ("\n\n", ""),
    ),
)
def test_first_line(pasted: str, expected: str):
    assert first_line(pasted) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (1, 1),
        (5, 5),
        (16, 16),
        # Outside the range: brought into it, not refused.
        (17, 16),
        (10**6, 16),
        (0, 1),
        (-3, 1),
        # What a settings file edited by hand can hold.
        ("12", 12),
        (3.9, 3),
        ("many", None),
        ("", None),
        (None, None),
        (float("inf"), None),
        (float("nan"), None),
    ),
)
def test_number_in_range(value: object, expected: int | None):
    assert number_in_range(value, 16) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("5", 5),
        (" 7 ", 7),
        ("+5", 5),
        ("-4", -4),
        # Read as it is written: a leading zero makes a number neither octal nor unreadable.
        ("08", 8),
        ("09", 9),
        ("010", 10),
        ("0012", 12),
        ("100000000000000000000", 10**20),
        # Not whole, but numbers.
        ("3.7", 3.7),
        ("1e3", 1000.0),
        # Too large to count with.
        ("1e999", float("inf")),
        pytest.param("9" * 5000, float("inf"), id="5000 digits"),
        # No decimal numbers, whatever Tcl or Python would make of them.
        ("0x10", None),
        ("0b11", None),
        ("0o17", None),
        ("1_0", None),
        ("\u0663", None),
        ("", None),
        ("-", None),
        ("4 4", None),
        ("many", None),
        ("nan", None),
        ("inf", None),
        ("-inf", None),
        ("Infinity", None),
    ),
)
def test_number_in_text(text: str, expected: float | None):
    assert number_in_text(text) == expected


class _FakeClipboard:
    """Stands in for the system clipboard, which belongs to whoever is running the tests."""

    def __init__(self, read, write) -> None:
        self.text = ""
        # The window's own two functions, for the one test that runs them against a stand-in for Tk.
        self.read = read
        self.write = write


@pytest.fixture()
def clipboard(monkeypatch: pytest.MonkeyPatch) -> _FakeClipboard:
    """Replace the window's only two ways to the clipboard before any window exists."""
    from bdfr.gui import BdfrGui

    fake = _FakeClipboard(BdfrGui._read_clipboard, BdfrGui._write_clipboard)
    monkeypatch.setattr(BdfrGui, "_read_clipboard", lambda _gui: fake.text)
    monkeypatch.setattr(BdfrGui, "_write_clipboard", lambda _gui, text: setattr(fake, "text", text))
    return fake


@pytest.fixture()
def window(tmp_path: Path, clipboard: _FakeClipboard):
    """The window laid out as on screen, which fails the test if one of its callbacks raised."""
    root = _new_tk_root()

    from bdfr.gui import BdfrGui

    callback_errors = []
    root.report_callback_exception = lambda *error: callback_errors.append(error)
    try:
        gui = BdfrGui(root, GuiSettings(usernames="alice\nbob", directory=str(tmp_path)))
        # The real pop-up waits for somebody to dismiss it.
        gui.context_menu.tk_popup = lambda *_position: None
        root.update()
        yield gui
        assert callback_errors == []
    finally:
        root.destroy()


def _text_inputs(gui) -> list:
    return [
        gui.text_usernames,
        gui.entry_directory,
        *gui.option_entries.values(),
        gui.spin_max_parallel,
        gui.spin_concurrency,
        *gui.api_entries.values(),
    ]


def _right_click(widget, x: int = 1, y: int = 1) -> None:
    widget.event_generate("<Button-3>", x=x, y=y)
    widget.event_generate("<ButtonRelease-3>", x=x, y=y)


def _right_click_at(widget, index) -> None:
    """Right-click the left half of the character at `index`, which is the gap in front of it."""
    left, top, _, _ = widget.bbox(index)
    _right_click(widget, left + 1, top + 1)


def _menu_state(gui, label: str) -> str:
    return str(gui.context_menu.entrycget(label, "state"))


def _names(gui) -> str:
    return gui.text_usernames.get("1.0", "end-1c")


@pytest.mark.gui
def test_every_text_input_has_the_right_click_menu(window):
    menu = window.context_menu
    labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) == "command"]
    assert labels == [MENU_CUT, MENU_COPY, MENU_PASTE, MENU_SELECT_ALL]
    assert int(menu.cget("tearoff")) == 0

    inputs = _text_inputs(window)
    # The name box, the folder, four options, two spinboxes and three API fields.
    assert len(inputs) == 11
    shown = []
    menu.tk_popup = lambda *position: shown.append(position)
    for widget in inputs:
        _right_click(widget, 7, 5)
        assert window._menu_target is widget
        # The menu opens where the click was, which it is told as a place on the screen.
        assert shown[-1] == (widget.winfo_rootx() + 7, widget.winfo_rooty() + 5)
        # The click also puts the keyboard focus in the field it was made on.
        assert str(window.root.tk.call("focus", "-lastfor", window.root)) == str(widget)
    assert len(shown) == len(inputs)


@pytest.mark.gui
def test_right_click_paste_goes_in_where_the_click_was(window, clipboard: _FakeClipboard):
    clipboard.text = "XY"
    names = window.text_usernames
    left, top, width, _ = names.bbox("1.2")
    _right_click(names, left + 1, top + 1)
    assert names.index("insert") == "1.2"
    window.context_menu.invoke(MENU_PASTE)
    assert _names(window) == "alXYice\nbob"
    # A click on the right half of a character belongs to the gap after it.
    left, top, width, _ = names.bbox("2.1")
    _right_click(names, left + width - 1, top + 1)
    assert names.index("insert") == "2.2"

    entry = window.option_entries["Folder scheme"]
    window.var_folder_scheme.set("0123456789")
    window.root.update()
    left, _, width, _ = entry.bbox(4)
    _right_click(entry, left + 1)
    assert entry.index("insert") == 4
    window.context_menu.invoke(MENU_PASTE)
    assert window.var_folder_scheme.get() == "0123XY456789"
    _right_click(entry, left + width - 1)
    assert entry.index("insert") == 5


@pytest.mark.gui
def test_right_click_past_the_end_of_a_line_stays_on_that_line(window, clipboard: _FakeClipboard):
    clipboard.text = "XY"
    names = window.text_usernames
    _, top, _, _ = names.bbox("1.0")
    _right_click(names, names.winfo_width() - 8, top + 1)
    window.context_menu.invoke(MENU_PASTE)
    assert _names(window) == "aliceXY\nbob"

    entry = window.entry_directory
    window.var_directory.set("out")
    window.root.update()
    _right_click(entry, entry.winfo_width() - 8)
    window.context_menu.invoke(MENU_PASTE)
    assert window.var_directory.get() == "outXY"


@pytest.mark.gui
def test_right_click_on_the_selection_pastes_over_it(window, clipboard: _FakeClipboard):
    clipboard.text = "carol"
    names = window.text_usernames
    names.focus_set()
    names.tag_add("sel", "2.0", "2.3")
    _right_click_at(names, "2.1")
    assert _menu_state(window, MENU_PASTE) == "normal"
    window.context_menu.invoke(MENU_PASTE)
    assert _names(window) == "alice\ncarol"

    # The end of the selection counts as on it, and with it the empty space after a selected name.
    clipboard.text = "dave"
    names.tag_add("sel", "2.0", "2.5")
    _, top, _, _ = names.bbox("2.0")
    _right_click(names, names.winfo_width() - 8, top + 1)
    assert names.get("sel.first", "sel.last") == "carol"
    window.context_menu.invoke(MENU_PASTE)
    assert _names(window) == "alice\ndave"

    clipboard.text = "carol"
    entry = window.option_entries["File scheme"]
    window.var_file_scheme.set("{REDDITOR}_{TITLE}")
    window.root.update()
    entry.focus_set()
    entry.selection_range(11, 18)
    _right_click_at(entry, 14)
    window.context_menu.invoke(MENU_PASTE)
    assert window.var_file_scheme.get() == "{REDDITOR}_carol"

    # Either end of the selection counts as on it, and with it the empty space after a field selected in full.
    entry.selection_range(0, "end")
    _right_click(entry, entry.winfo_width() - 8)
    window.context_menu.invoke(MENU_PASTE)
    assert window.var_file_scheme.get() == "carol"

    clipboard.text = "12"
    spinbox = window.spin_concurrency
    spinbox.focus_set()
    spinbox.selection_range(0, "end")
    _right_click(spinbox)
    window.context_menu.invoke(MENU_PASTE)
    assert window.var_concurrency.get() == 12


@pytest.mark.gui
def test_right_click_away_from_the_selection_drops_it_and_pastes_at_the_click(window, clipboard: _FakeClipboard):
    clipboard.text = "XY"
    names = window.text_usernames
    names.focus_set()
    names.tag_add("sel", "2.0", "2.3")
    _right_click_at(names, "1.2")
    assert not names.tag_ranges("sel")
    assert names.index("insert") == "1.2"
    # With the selection gone there is nothing left to cut or copy.
    assert _menu_state(window, MENU_CUT) == "disabled"
    assert _menu_state(window, MENU_COPY) == "disabled"
    window.context_menu.invoke(MENU_PASTE)
    assert _names(window) == "alXYice\nbob"

    entry = window.option_entries["Folder scheme"]
    window.var_folder_scheme.set("0123456789")
    window.root.update()
    entry.focus_set()
    entry.selection_range(6, 9)
    _right_click_at(entry, 2)
    assert not entry.selection_present()
    window.context_menu.invoke(MENU_PASTE)
    assert window.var_folder_scheme.get() == "01XY23456789"


@pytest.mark.gui
def test_selection_left_behind_in_a_field_is_not_pasted_over(window, clipboard: _FakeClipboard):
    """Windows does not show the selection of a field without the focus, so a click on it is not aimed at it."""
    clipboard.text = "XY"
    names = window.text_usernames
    names.focus_set()
    names.tag_add("sel", "2.0", "2.3")
    window.entry_directory.focus_set()
    _right_click_at(names, "2.1")
    assert not names.tag_ranges("sel")
    window.context_menu.invoke(MENU_PASTE)
    assert _names(window) == "alice\nbXYob"

    # What tabbing on through the fields leaves behind: all of the last one selected.
    entry = window.option_entries["File scheme"]
    window.var_file_scheme.set("0123456789")
    window.root.update()
    entry.focus_set()
    entry.selection_range(0, "end")
    window.option_entries["Limit per user (blank = all)"].focus_set()
    _right_click_at(entry, 5)
    assert not entry.selection_present()
    window.context_menu.invoke(MENU_PASTE)
    assert window.var_file_scheme.get() == "01234XY56789"


@pytest.mark.gui
def test_paste_is_disabled_while_the_clipboard_holds_no_text(window, clipboard: _FakeClipboard):
    for widget in (window.text_usernames, window.entry_directory, window.api_entries["Client secret"]):
        clipboard.text = ""
        _right_click(widget)
        assert _menu_state(window, MENU_PASTE) == "disabled"
        clipboard.text = "something"
        _right_click(widget)
        assert _menu_state(window, MENU_PASTE) == "normal"
    # A one-line field has nothing to take from line breaks alone.
    clipboard.text = "\n\n"
    _right_click(window.entry_directory)
    assert _menu_state(window, MENU_PASTE) == "disabled"

    clipboard.text = ""
    before = window.collect_settings()
    for widget in _text_inputs(window):
        window._menu_target = widget
        window._menu_paste()
    assert window.collect_settings() == before


@pytest.mark.gui
def test_one_line_field_takes_the_first_pasted_line(window, clipboard: _FakeClipboard):
    clipboard.text = "\nmy-client-id\nsomething else\n"
    entry = window.api_entries["Client ID"]
    _right_click(entry)
    window.context_menu.invoke(MENU_PASTE)
    assert window.var_client_id.get() == "my-client-id"


@pytest.mark.gui
def test_spinbox_takes_a_pasted_whole_number_or_nothing(window, clipboard: _FakeClipboard):
    """Other text would sit in the box as something neither a start nor the running downloads can use."""
    for spinbox in (window.spin_max_parallel, window.spin_concurrency):
        before = spinbox.get()
        for unusable in ("some_user", "1.5", "-3", "1e3", "12 13", "inf", "\u0663", "\n\n", ""):
            clipboard.text = unusable
            _right_click(spinbox)
            assert _menu_state(window, MENU_PASTE) == "disabled", unusable
            # Not through the command behind the disabled entry either.
            window._menu_paste()
            assert spinbox.get() == before

    clipboard.text = " 12 \nthe rest\n"
    spinbox = window.spin_concurrency
    spinbox.focus_set()
    spinbox.selection_range(0, "end")
    _right_click(spinbox)
    assert _menu_state(window, MENU_PASTE) == "normal"
    window.context_menu.invoke(MENU_PASTE)
    assert spinbox.get() == "12"


@pytest.mark.gui
def test_cut_and_copy_need_a_selection_and_use_it(window, clipboard: _FakeClipboard):
    names = window.text_usernames
    _right_click(names)
    assert _menu_state(window, MENU_CUT) == "disabled"
    assert _menu_state(window, MENU_COPY) == "disabled"

    names.tag_add("sel", "1.0", "1.5")
    _right_click(names)
    assert _menu_state(window, MENU_CUT) == "normal"
    assert _menu_state(window, MENU_COPY) == "normal"
    window.context_menu.invoke(MENU_COPY)
    assert clipboard.text == "alice"
    assert _names(window) == "alice\nbob"

    names.tag_remove("sel", "1.0", "end")
    names.tag_add("sel", "1.0", "2.0")
    _right_click(names)
    window.context_menu.invoke(MENU_CUT)
    assert clipboard.text == "alice\n"
    assert _names(window) == "bob"

    entry = window.option_entries["Skip extensions"]
    window.var_skip.set("gif, avi")
    entry.focus_set()
    entry.selection_range(0, 3)
    _right_click(entry)
    window.context_menu.invoke(MENU_COPY)
    assert clipboard.text == "gif"
    window.context_menu.invoke(MENU_CUT)
    assert clipboard.text == "gif"
    assert window.var_skip.get() == ", avi"


@pytest.mark.gui
def test_client_secret_cannot_be_copied_out_of_its_masked_field(window, clipboard: _FakeClipboard):
    clipboard.text = "untouched"
    window.var_client_secret.set("sentinel-secret-value")
    secret = window.api_entries["Client secret"]
    secret.focus_set()
    secret.selection_range(0, "end")
    _right_click(secret)
    assert secret.selection_present()
    assert _menu_state(window, MENU_CUT) == "disabled"
    assert _menu_state(window, MENU_COPY) == "disabled"
    # Not through the commands behind the disabled entries either.
    window._menu_cut()
    window._menu_copy()
    assert clipboard.text == "untouched"
    assert window.var_client_secret.get() == "sentinel-secret-value"

    # Pasting a secret in is what the field is for.
    assert _menu_state(window, MENU_PASTE) == "normal"
    window.context_menu.invoke(MENU_PASTE)
    assert window.var_client_secret.get() == "untouched"

    # The unmasked field beside it can be copied from.
    window.var_client_id.set("my-client-id")
    window.api_entries["Client ID"].focus_set()
    window.api_entries["Client ID"].selection_range(0, "end")
    _right_click(window.api_entries["Client ID"])
    assert _menu_state(window, MENU_COPY) == "normal"


@pytest.mark.gui
def test_select_all_selects_the_whole_field(window, clipboard: _FakeClipboard):
    _right_click(window.text_usernames)
    window.context_menu.invoke(MENU_SELECT_ALL)
    assert window.text_usernames.get("sel.first", "sel.last") == "alice\nbob"
    # The next right click finds all of it selected, ready to be copied.
    _right_click(window.text_usernames)
    window.context_menu.invoke(MENU_COPY)
    assert clipboard.text == "alice\nbob"
    # So does a click below the last name, which lands at the end of the list and with that of the selection.
    clipboard.text = ""
    _right_click(window.text_usernames, 5, window.text_usernames.winfo_height() - 4)
    assert window.text_usernames.get("sel.first", "sel.last") == "alice\nbob"
    assert _menu_state(window, MENU_COPY) == "normal"
    window.context_menu.invoke(MENU_COPY)
    assert clipboard.text == "alice\nbob"

    entry = window.option_entries["Folder scheme"]
    _right_click(entry)
    window.context_menu.invoke(MENU_SELECT_ALL)
    assert (entry.index("sel.first"), entry.index("sel.last")) == (0, len(window.var_folder_scheme.get()))
    _right_click(entry)
    window.context_menu.invoke(MENU_COPY)
    assert clipboard.text == window.var_folder_scheme.get()


@pytest.mark.gui
def test_paste_longer_than_its_field_keeps_the_cursor_in_view(window, clipboard: _FakeClipboard):
    """As typing does: the end of what was pasted is where the work goes on."""
    entry = window.option_entries["Folder scheme"]
    window.var_folder_scheme.set("")
    window.root.update()
    clipboard.text = "x" * 300
    _right_click(entry)
    window.context_menu.invoke(MENU_PASTE)
    window.root.update()
    assert entry.index("insert") == 300
    left, _, width, _ = entry.bbox("insert")
    assert 0 <= left and left + width <= entry.winfo_width()


@pytest.mark.gui
def test_clipboard_is_read_and_written_through_tk(window, clipboard: _FakeClipboard):
    """The two functions that every other test replaces, run against a stand-in for Tk's clipboard calls.

    The stand-ins are set on the root window itself, where they are found
    before Tk's own methods, so the real clipboard is not reached.
    """
    import tkinter as tk

    held = ["from elsewhere"]
    calls = []

    def get() -> str:
        if held[0] is None:
            raise tk.TclError("CLIPBOARD selection does not exist")
        return held[0]

    def clear() -> None:
        calls.append("clear")
        if held[0] == "in use":
            raise tk.TclError("the clipboard is in use")

    window.root.clipboard_get = get
    window.root.clipboard_clear = clear
    window.root.clipboard_append = lambda text: calls.append(("append", text))

    assert clipboard.read(window) == "from elsewhere"
    # An empty clipboard, like one that holds a picture, is no text to Tk and no error to the window.
    held[0] = None
    assert clipboard.read(window) == ""

    # What was on it is replaced, not added to.
    clipboard.write(window, "copied")
    assert calls == ["clear", ("append", "copied")]
    # A clipboard that cannot be written to costs the copy, not the window.
    held[0] = "in use"
    clipboard.write(window, "again")
    assert calls == ["clear", ("append", "copied"), "clear"]


@pytest.mark.gui
def test_menu_lets_go_of_the_pointer_once_it_has_gone(window):
    import tkinter as tk

    released = []
    window.context_menu.grab_release = lambda: released.append(True)
    window._menu_outlasts_popup = False
    _right_click(window.text_usernames)
    assert released == [True]

    # Also when the menu could not be shown at all.
    def no_menu(*_position):
        raise tk.TclError("no menu")

    window.context_menu.tk_popup = no_menu
    click = SimpleNamespace(widget=window.text_usernames, x=1, y=1, x_root=0, y_root=0)
    with pytest.raises(tk.TclError, match="no menu"):
        window._show_context_menu(click)
    assert released == [True, True]


@pytest.mark.gui
def test_menu_keeps_its_grab_where_it_is_still_up(window):
    """On X11 the menu outlasts the call that shows it, and its grab is what lets a click elsewhere close it."""
    assert window._menu_outlasts_popup == (window.root.tk.call("tk", "windowingsystem") == "x11")
    released = []
    window.context_menu.grab_release = lambda: released.append(True)
    window._menu_outlasts_popup = True
    _right_click(window.text_usernames)
    assert window._menu_target is window.text_usernames
    assert released == []


@pytest.mark.gui
def test_undo_takes_back_a_paste_in_one_step(window, clipboard: _FakeClipboard):
    clipboard.text = "carol"
    names = window.text_usernames
    names.focus_set()
    names.tag_add("sel", "2.0", "2.3")
    _right_click_at(names, "2.1")
    window.context_menu.invoke(MENU_PASTE)
    assert _names(window) == "alice\ncarol"
    names.edit_undo()
    assert _names(window) == "alice\nbob"

    # A name typed just before, which to the widget is the same kind of edit as the paste that
    # follows: it would be undone along with the paste if the paste were not a step of its own.
    names.insert("end", "\ndave")
    window.button_paste.invoke()
    assert _names(window) == "alice\nbob\ndave\ncarol"
    names.edit_undo()
    assert _names(window) == "alice\nbob\ndave"
    # Typing afterwards still makes undo steps of its own.
    assert bool(names.cget("autoseparators")) is True


@pytest.mark.gui
@pytest.mark.parametrize(
    ("existing", "pasted", "expected"),
    (
        ("", "alice", "alice"),
        ("alice", "bob", "alice\nbob"),
        ("alice\n", "bob", "alice\nbob"),
        ("alice", "bob\ncarol\n", "alice\nbob\ncarol"),
        ("alice", "\n  bob  \n\n", "alice\nbob"),
        # A blank line the user left is kept, and no second one is added to it.
        ("alice\n\n", "bob", "alice\n\nbob"),
        ("alice", "", "alice"),
        ("alice", " \n\t\n", "alice"),
    ),
)
def test_paste_button_appends_the_clipboard_on_a_new_line(
    existing: str, pasted: str, expected: str, window, clipboard: _FakeClipboard
):
    window.text_usernames.delete("1.0", "end")
    window.text_usernames.insert("1.0", existing)
    clipboard.text = pasted
    window.button_paste.invoke()
    assert _names(window) == expected


@pytest.mark.gui
def test_paste_button_can_be_pressed_again_and_again(window, clipboard: _FakeClipboard):
    for name in ("carol\n", "dave\n", "erin\n"):
        clipboard.text = name
        window.button_paste.invoke()
    assert _names(window) == "alice\nbob\ncarol\ndave\nerin"
    assert parse_usernames(window.collect_settings().usernames) == ["alice", "bob", "carol", "dave", "erin"]


@pytest.mark.gui
def test_paste_button_shows_what_it_added(window, clipboard: _FakeClipboard):
    names = window.text_usernames
    names.insert("end", "\n" + "\n".join(f"user{i}" for i in range(80)))
    names.yview_moveto(0)
    window.root.update()
    assert names.bbox("end-1c") is None, "the end of the list is meant to be out of view"

    clipboard.text = "newest"
    # The cursor is somewhere else, as it is after a click into the list.
    names.mark_set("insert", "1.2")
    window.button_paste.invoke()
    window.root.update()
    assert names.bbox("end-1c") is not None
    assert names.index("insert") == names.index("end-1c")


@pytest.mark.gui
def test_paste_button_does_not_make_the_window_taller(window):
    """It shares the hint's row in the name box, which is shorter than the options beside it."""
    footer = window.button_paste.master
    assert [str(child.winfo_class()) for child in footer.grid_slaves(row=0)] == ["TButton", "TLabel"]
    tallest = max(child.winfo_reqheight() for child in window.top_frame.grid_slaves(row=0))
    assert window.names_box.winfo_reqheight() < tallest


def _run_window_for(gui, seconds: float) -> None:
    """Let the window's event loop run, as it does on screen."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        gui.root.update()
        time.sleep(0.005)


def _type_over(spinbox, text: str) -> None:
    """Put `text` in place of what a spinbox holds, as typing does: nothing has entered it yet."""
    spinbox.delete(0, "end")
    spinbox.insert(0, text)


def _enter(spinbox) -> None:
    """Enter what a spinbox holds by leaving the box.

    The keyboard focus going elsewhere reaches the box as a <FocusOut> event,
    and that event is what is sent here. Moving the focus for real, like
    pressing a key, only does anything in a window that has the keyboard,
    which a test run cannot count on.
    """
    spinbox.event_generate("<FocusOut>")


def _click_arrow(spinbox, arrow: str) -> None:
    """Click the "uparrow" or the "downarrow" of a spinbox, wherever the theme in use has put it."""
    spinbox.update()
    height = spinbox.winfo_height()
    for x in range(spinbox.winfo_width() - 1, -1, -1):
        for y in range(height):
            part = str(spinbox.identify(x, y))
            # Some themes draw one button for both arrows, whose upper half counts up.
            in_shared_button = part.endswith("spinbutton") and (2 * y < height) == (arrow == "uparrow")
            if part.endswith(arrow) or in_shared_button:
                spinbox.event_generate("<ButtonPress-1>", x=x, y=y)
                spinbox.event_generate("<ButtonRelease-1>", x=x, y=y)
                return
    raise AssertionError(f"the spinbox shows no {arrow}")


def _numbers_passed_on(setter: MagicMock) -> list[int]:
    return [passed.args[0] for passed in setter.mock_calls]


@pytest.fixture()
def running(window):
    """The window with downloads under way, whose manager records what the spinboxes pass on to it."""
    window.manager = MagicMock(ordered_jobs=[])
    return window


@pytest.mark.gui
def test_arrows_of_a_spinbox_apply_at_once_to_the_downloads_that_are_running(running):
    manager = running.manager
    simultaneous, connections = running.spin_max_parallel, running.spin_concurrency
    assert (simultaneous.get(), connections.get()) == ("3", "4")

    _click_arrow(simultaneous, "uparrow")
    assert simultaneous.get() == "4"
    assert _numbers_passed_on(manager.set_max_parallel) == [4]
    assert _numbers_passed_on(manager.set_concurrency) == []

    _click_arrow(connections, "downarrow")
    _click_arrow(connections, "downarrow")
    assert connections.get() == "2"
    assert _numbers_passed_on(manager.set_concurrency) == [3, 2]
    assert _numbers_passed_on(manager.set_max_parallel) == [4]

    # The arrows stop at the ends of the range.
    _type_over(simultaneous, str(MAX_PARALLEL_JOBS - 1))
    _enter(simultaneous)
    _click_arrow(simultaneous, "uparrow")
    _click_arrow(simultaneous, "uparrow")
    assert simultaneous.get() == str(MAX_PARALLEL_JOBS)
    assert _numbers_passed_on(manager.set_max_parallel) == [4, MAX_PARALLEL_JOBS - 1, MAX_PARALLEL_JOBS]
    _click_arrow(connections, "downarrow")
    _click_arrow(connections, "downarrow")
    assert connections.get() == "1"
    assert _numbers_passed_on(manager.set_concurrency) == [3, 2, 1]


@pytest.mark.gui
def test_step_does_not_start_from_a_number_outside_the_range_that_is_being_typed(running):
    """Tk's own step makes 16 of the "35" on the way from 3 to 5, even by the arrow that is meant to lower it."""
    manager = running.manager
    simultaneous = running.spin_max_parallel
    assert simultaneous.get() == "3"

    simultaneous.insert("end", "5")
    assert simultaneous.get() == "35"
    _click_arrow(simultaneous, "downarrow")
    assert simultaneous.get() == "2"
    assert _numbers_passed_on(manager.set_max_parallel) == [2]

    # Text that is no number, and no text at all, are stepped away from in the same way: from the number entered last.
    for text, arrow, expected in (
        ("", "uparrow", 3),
        ("many", "uparrow", 4),
        ("inf", "downarrow", 3),
        ("99", "downarrow", 2),
        ("0", "uparrow", 3),
        ("0x10", "uparrow", 4),
    ):
        _type_over(simultaneous, text)
        _click_arrow(simultaneous, arrow)
        assert simultaneous.get() == str(expected), text
    assert _numbers_passed_on(manager.set_max_parallel) == [2, 3, 4, 3, 2, 3, 4]

    # A number inside the range is what a step starts from, entered or not, and it is read as it is written.
    _type_over(simultaneous, "08")
    _click_arrow(simultaneous, "uparrow")
    assert simultaneous.get() == "9"
    _type_over(simultaneous, "12")
    _click_arrow(simultaneous, "downarrow")
    assert simultaneous.get() == "11"
    assert _numbers_passed_on(manager.set_max_parallel) == [2, 3, 4, 3, 2, 3, 4, 9, 11]
    assert _numbers_passed_on(manager.set_concurrency) == []


@pytest.mark.gui
def test_up_and_down_keys_step_a_spinbox_as_its_arrows_do(running):
    manager = running.manager
    connections = running.spin_concurrency
    # A key press only reaches a window that has the keyboard, which a test run cannot count on, so
    # the events that Tk makes of the two keys are sent in their place.
    assert STEP_UP in connections.bind_class("TSpinbox", "<Key-Up>")
    assert STEP_DOWN in connections.bind_class("TSpinbox", "<Key-Down>")
    assert {STEP_UP, STEP_DOWN} <= set(connections.bind())

    assert connections.get() == "4"
    connections.insert("end", "5")
    connections.event_generate(STEP_UP)
    assert connections.get() == "5"
    connections.event_generate(STEP_DOWN)
    connections.event_generate(STEP_DOWN)
    assert connections.get() == "3"
    assert _numbers_passed_on(manager.set_concurrency) == [5, 4, 3]
    assert _numbers_passed_on(manager.set_max_parallel) == []


@pytest.mark.gui
def test_mouse_wheel_does_not_change_a_spinbox(running):
    """Tk makes a step of every notch, also over a box that is not in use, and a number that went up stays up."""
    manager = running.manager
    running.entry_directory.focus_set()
    for spinbox in (running.spin_max_parallel, running.spin_concurrency):
        assert set(MOUSE_WHEEL) <= set(spinbox.bind())
        before = spinbox.get()
        for delta in (120, 120, 240, -120):
            spinbox.event_generate("<MouseWheel>", delta=delta)
        assert spinbox.get() == before
        # Not while the box is the one in use either.
        spinbox.focus_set()
        spinbox.event_generate("<MouseWheel>", delta=120)
        spinbox.event_generate("<MouseWheel>", delta=-120)
        assert spinbox.get() == before
    assert manager.set_max_parallel.mock_calls == []
    assert manager.set_concurrency.mock_calls == []


@pytest.mark.gui
def test_number_being_typed_is_not_passed_on_until_it_is_entered(running):
    """A number that was too high cannot be taken back, so the 35 on the way from 3 to 5 must not be acted on."""
    manager = running.manager
    simultaneous, connections = running.spin_max_parallel, running.spin_concurrency
    assert (simultaneous.get(), connections.get()) == ("3", "4")

    # A 5 typed behind the 3 to raise it, and a 1 typed in front of the 4 to lower it ...
    simultaneous.insert("end", "5")
    connections.insert(0, "1")
    assert (simultaneous.get(), connections.get()) == ("35", "14")
    # ... left standing for as long as it takes, and then the old digit taken away.
    _run_window_for(running, 0.3)
    simultaneous.delete(0, 1)
    connections.delete(1, "end")
    _run_window_for(running, 0.3)
    assert (simultaneous.get(), connections.get()) == ("5", "1")
    assert _numbers_passed_on(manager.set_max_parallel) == []
    assert _numbers_passed_on(manager.set_concurrency) == []

    _enter(simultaneous)
    assert _numbers_passed_on(manager.set_max_parallel) == [5]
    assert _numbers_passed_on(manager.set_concurrency) == []
    _enter(connections)
    assert _numbers_passed_on(manager.set_concurrency) == [1]
    assert _numbers_passed_on(manager.set_max_parallel) == [5]


@pytest.mark.gui
def test_typed_number_is_entered_by_the_enter_keys_and_by_leaving_the_box(running):
    assert NUMBER_ENTERED_BY == ("<Return>", "<KP_Enter>", "<FocusOut>")
    assert set(running.root.event_info(NUMBER_ENTERED)) == {"<Key-Return>", "<Key-KP_Enter>", "<FocusOut>"}
    manager = running.manager
    for spinbox, setter in (
        (running.spin_max_parallel, manager.set_max_parallel),
        (running.spin_concurrency, manager.set_concurrency),
    ):
        assert NUMBER_ENTERED in spinbox.bind()
        # Leaving the box, as the event Tk sends to it when the focus goes elsewhere.
        _type_over(spinbox, "7")
        assert _numbers_passed_on(setter) == []
        spinbox.event_generate("<FocusOut>")
        assert _numbers_passed_on(setter) == [7]
        # A key press only reaches a window that has the keyboard, which a test run cannot count
        # on, so for the Enter keys the event they stand for is sent in their place.
        _type_over(spinbox, "9")
        assert _numbers_passed_on(setter) == [7]
        spinbox.event_generate(NUMBER_ENTERED)
        assert _numbers_passed_on(setter) == [7, 9]


@pytest.mark.gui
def test_number_pasted_into_a_spinbox_is_not_passed_on_until_it_is_entered(running, clipboard: _FakeClipboard):
    manager = running.manager
    spinbox = running.spin_concurrency
    clipboard.text = "12"
    spinbox.focus_set()
    spinbox.selection_range(0, "end")
    _right_click(spinbox)
    running.context_menu.invoke(MENU_PASTE)
    _run_window_for(running, 0.3)
    assert spinbox.get() == "12"
    assert _numbers_passed_on(manager.set_concurrency) == []
    _enter(spinbox)
    assert _numbers_passed_on(manager.set_concurrency) == [12]

    # Pasted behind a number, it makes one that is out of range for as long as it stands unentered.
    clipboard.text = "5"
    left, top, width, _ = spinbox.bbox(1)
    _right_click(spinbox, left + width - 1, top + 1)
    running.context_menu.invoke(MENU_PASTE)
    _run_window_for(running, 0.3)
    assert spinbox.get() == "125"
    assert _numbers_passed_on(manager.set_concurrency) == [12]
    _enter(spinbox)
    assert _numbers_passed_on(manager.set_concurrency) == [12, MAX_CONCURRENCY]
    assert spinbox.get() == str(MAX_CONCURRENCY)

    # A whole number is taken as it is written: a leading zero makes it neither octal nor unusable.
    clipboard.text = "08"
    spinbox.selection_range(0, "end")
    _right_click(spinbox)
    assert _menu_state(running, MENU_PASTE) == "normal"
    running.context_menu.invoke(MENU_PASTE)
    assert spinbox.get() == "08"
    _enter(spinbox)
    assert spinbox.get() == "8"
    assert _numbers_passed_on(manager.set_concurrency) == [12, MAX_CONCURRENCY, 8]
    assert _numbers_passed_on(manager.set_max_parallel) == []


@pytest.mark.gui
def test_keyboard_paste_into_a_spinbox_takes_a_whole_number_or_nothing(running, clipboard: _FakeClipboard):
    """Ctrl+V and Shift+Insert, which Tk's own paste would let put any text into the box."""
    manager = running.manager
    # The keys reach a field as <<Paste>>. Sending that event here would hand a mistake in the
    # binding to Tk's own paste, and with it to the real clipboard, so the function bound to it is called.
    assert "<<Paste>>" not in running.entry_directory.bind()
    for spinbox, setter in (
        (running.spin_max_parallel, manager.set_max_parallel),
        (running.spin_concurrency, manager.set_concurrency),
    ):
        assert "_paste_from_keyboard" in spinbox.bind("<<Paste>>")
        keys = SimpleNamespace(widget=spinbox)
        before = spinbox.get()
        for unusable in ("some_user", "1.5", "-3", "1e3", "12 13", "inf", "\u0663", "\n\n", ""):
            clipboard.text = unusable
            # Tk's own paste is kept from running after it, whatever is on the clipboard.
            assert running._paste_from_keyboard(keys) == "break"
            assert spinbox.get() == before, unusable

        clipboard.text = " 09 \nthe rest\n"
        spinbox.selection_range(0, "end")
        assert running._paste_from_keyboard(keys) == "break"
        assert spinbox.get() == "09"
        # Like a number from the menu, it is not passed on until it is entered.
        assert _numbers_passed_on(setter) == []
        _enter(spinbox)
        assert spinbox.get() == "9"
        assert _numbers_passed_on(setter) == [9]


@pytest.mark.gui
def test_menu_that_takes_the_keyboard_does_not_enter_the_number_under_it(running, clipboard: _FakeClipboard):
    """On X11 the menu has the keyboard focus while it is up, and the box it was opened on is told it has lost it."""
    manager = running.manager
    spinbox = running.spin_max_parallel

    def menu_has_keyboard() -> str:
        return str(running.context_menu)

    clipboard.text = "12"
    _type_over(spinbox, "")
    _right_click(spinbox)
    running._keyboard_widget = menu_has_keyboard
    spinbox.event_generate("<FocusOut>")
    # The box has not been left: nothing is entered, and the number entered last is not put back into it.
    assert spinbox.get() == ""
    assert _numbers_passed_on(manager.set_max_parallel) == []
    running.context_menu.invoke(MENU_PASTE)
    assert spinbox.get() == "12"
    # With the menu gone the keyboard is back in the box, and leaving it enters the number.
    del running._keyboard_widget
    assert running._keyboard_widget() != str(running.context_menu)
    _enter(spinbox)
    assert _numbers_passed_on(manager.set_max_parallel) == [12]

    # A menu opened on another field is no reason to wait: the click that opened it has left the box.
    _type_over(spinbox, "5")
    _right_click(running.entry_directory)
    running._keyboard_widget = menu_has_keyboard
    spinbox.event_generate("<FocusOut>")
    assert _numbers_passed_on(manager.set_max_parallel) == [12, 5]


@pytest.mark.gui
def test_click_beside_a_spinbox_enters_the_number_typed_into_it(running):
    """A label, a frame and the window itself take no keyboard focus, so a click on them would not leave the box."""
    manager = running.manager
    spinbox = running.spin_concurrency
    options = spinbox.master
    label = next(child for child in options.winfo_children() if child.winfo_class() == "TLabel")

    def in_use() -> str:
        return str(running.root.tk.call("focus", "-lastfor", running.root))

    typed = []
    for beside in (label, options, running.names_box, running.actions_bar, running.root):
        typed.append(len(typed) + 6)
        spinbox.focus_set()
        _type_over(spinbox, str(typed[-1]))
        assert in_use() == str(spinbox)
        beside.event_generate("<Button-1>", x=1, y=1)
        beside.event_generate("<ButtonRelease-1>", x=1, y=1)
        assert _numbers_passed_on(manager.set_concurrency) == typed
        # The box has been left: what is typed next does not go into it.
        assert in_use() == str(running.root)

    # A click into the box itself is part of typing into it.
    spinbox.focus_set()
    _type_over(spinbox, "20")
    spinbox.event_generate("<Button-1>", x=3, y=3)
    spinbox.event_generate("<ButtonRelease-1>", x=3, y=3)
    assert in_use() == str(spinbox)
    assert spinbox.get() == "20"
    assert _numbers_passed_on(manager.set_concurrency) == typed
    assert _numbers_passed_on(manager.set_max_parallel) == []


@pytest.mark.gui
def test_entered_number_outside_a_spinbox_range_is_brought_into_it(running):
    """The range of a spinbox only holds its arrows back, not what is typed or pasted into it."""
    manager = running.manager
    for typed, simultaneous, connections in (
        ("35", MAX_PARALLEL_JOBS, MAX_CONCURRENCY),
        ("500", MAX_PARALLEL_JOBS, MAX_CONCURRENCY),
        ("1e3", MAX_PARALLEL_JOBS, MAX_CONCURRENCY),
        ("100000000000000000000", MAX_PARALLEL_JOBS, MAX_CONCURRENCY),
        ("0", 1, 1),
        ("-4", 1, 1),
        (" 7 ", 7, 7),
        # Read as they are written: a leading zero makes a number neither octal nor unusable.
        ("08", 8, 8),
        ("09", 9, 9),
        ("010", 10, 10),
        ("012", 12, 12),
        ("+5", 5, 5),
        ("3.7", 3, 3),
    ):
        # Each time from a number that all of these differ from.
        for spinbox in (running.spin_max_parallel, running.spin_concurrency):
            _type_over(spinbox, "2")
            _enter(spinbox)
        manager.reset_mock()
        _type_over(running.spin_max_parallel, typed)
        _type_over(running.spin_concurrency, typed)
        assert _numbers_passed_on(manager.set_max_parallel) == [], typed
        assert _numbers_passed_on(manager.set_concurrency) == [], typed
        _enter(running.spin_max_parallel)
        _enter(running.spin_concurrency)
        assert _numbers_passed_on(manager.set_max_parallel) == [simultaneous], typed
        assert _numbers_passed_on(manager.set_concurrency) == [connections], typed
        # The boxes show what the downloads were told, not what was typed.
        shown = (running.spin_max_parallel.get(), running.spin_concurrency.get())
        assert shown == (str(simultaneous), str(connections)), typed


@pytest.mark.gui
def test_entered_text_that_is_no_number_gives_way_to_the_number_entered_last(running):
    manager = running.manager
    simultaneous, connections = running.spin_max_parallel, running.spin_concurrency

    # The number the window was opened with stands in at first ...
    for text in ("", "x", "-", "4 4", "nan", "inf", "-inf", "Infinity", "1e999", "0x10", "0b11", "1_0", "\u0663"):
        _type_over(simultaneous, text)
        _type_over(connections, text)
        # Left as it is, for whoever is typing to carry on with, until it is entered.
        assert (simultaneous.get(), connections.get()) == (text, text)
        _enter(simultaneous)
        _enter(connections)
        assert (simultaneous.get(), connections.get()) == ("3", "4"), text
        # ... and the downloads, which have that number already, are told nothing.
        assert _numbers_passed_on(manager.set_max_parallel) == [], text
        assert _numbers_passed_on(manager.set_concurrency) == [], text

    # Once a number has been entered, that one stands in.
    _type_over(simultaneous, "6")
    _type_over(connections, "12")
    _enter(simultaneous)
    _enter(connections)
    _type_over(simultaneous, "many")
    _type_over(connections, "")
    _enter(simultaneous)
    _enter(connections)
    assert (simultaneous.get(), connections.get()) == ("6", "12")
    assert _numbers_passed_on(manager.set_max_parallel) == [6]
    assert _numbers_passed_on(manager.set_concurrency) == [12]


@pytest.mark.gui
def test_spinboxes_can_be_edited_while_nothing_is_running(window):
    assert window.manager is None
    spinbox = window.spin_concurrency
    _type_over(spinbox, "7")
    _enter(spinbox)
    assert spinbox.get() == "7"
    _click_arrow(spinbox, "uparrow")
    assert spinbox.get() == "8"
    _type_over(spinbox, "99")
    _enter(spinbox)
    assert spinbox.get() == str(MAX_CONCURRENCY)
    _type_over(spinbox, "")
    _enter(spinbox)
    assert spinbox.get() == str(MAX_CONCURRENCY)
    assert window.collect_settings().concurrency == MAX_CONCURRENCY


@pytest.mark.gui
def test_spinboxes_stop_at_the_most_that_can_be_used(window):
    assert int(window.spin_concurrency.cget("to")) == MAX_CONCURRENCY
    assert int(window.spin_max_parallel.cget("to")) == MAX_PARALLEL_JOBS


class _FakeProcess:
    """Stands in for a job's process, so that the scheduler can start jobs without anything running."""

    pid = 0
    exitcode = None

    def __init__(self, **_options):
        self._alive = False

    def start(self) -> None:
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self._alive = False

    def join(self, timeout: float | None = None) -> None:
        pass


@pytest.mark.gui
def test_typing_a_number_starts_no_more_jobs_than_were_asked_for(window):
    """The whole way from the spinbox to the scheduler, which starts queued jobs on every poll of the window."""
    manager = JobManager(max_parallel=3)
    manager._context = SimpleNamespace(
        Process=_FakeProcess, Value=lambda _typecode, value, lock=True: SimpleNamespace(value=value)
    )
    for index in range(MAX_PARALLEL_JOBS + 4):
        manager.add_job(f"user{index}", Configuration())
    window.manager = manager
    spinbox = window.spin_max_parallel
    try:
        _run_window_for(window, 0.5)
        assert manager.running_count == 3

        # From 3 to 5 by typing the 5 behind the 3 and then taking the 3 away.
        spinbox.insert("end", "5")
        _run_window_for(window, 0.5)
        assert manager.running_count == 3
        spinbox.delete(0, 1)
        _enter(spinbox)
        _run_window_for(window, 0.5)
        assert manager.running_count == 5

        # A number above the range starts no more jobs than the range allows, even when it is entered.
        spinbox.insert("end", "0")
        _enter(spinbox)
        _run_window_for(window, 0.5)
        assert manager.max_parallel == MAX_PARALLEL_JOBS
        assert manager.running_count == MAX_PARALLEL_JOBS
        assert spinbox.get() == str(MAX_PARALLEL_JOBS)
    finally:
        manager.shutdown()


@pytest.mark.gui
def test_settings_are_collected_from_spinboxes_that_hold_no_number(window):
    _type_over(window.spin_concurrency, "")
    _type_over(window.spin_max_parallel, "many")
    collected = window.collect_settings()
    # The numbers the window was opened with stand in.
    assert (collected.concurrency, collected.max_parallel_jobs) == (DEFAULT_CONCURRENCY, DEFAULT_MAX_PARALLEL_JOBS)

    # Once a number has been entered, that one does.
    _type_over(window.spin_concurrency, "9")
    _enter(window.spin_concurrency)
    _type_over(window.spin_concurrency, "")
    assert window.collect_settings().concurrency == 9

    # A number that has not been entered yet is the one to collect, brought into its range.
    _type_over(window.spin_concurrency, "500")
    _type_over(window.spin_max_parallel, "2")
    collected = window.collect_settings()
    assert (collected.concurrency, collected.max_parallel_jobs) == (MAX_CONCURRENCY, 2)
    # Collecting changes nothing in the window.
    assert (window.spin_concurrency.get(), window.spin_max_parallel.get()) == ("500", "2")

    # A number is collected as it is written, a leading zero included.
    _type_over(window.spin_concurrency, "010")
    _type_over(window.spin_max_parallel, "08")
    collected = window.collect_settings()
    assert (collected.concurrency, collected.max_parallel_jobs) == (10, 8)


@pytest.mark.gui
def test_start_uses_and_shows_a_usable_number_for_each_spinbox(window, monkeypatch: pytest.MonkeyPatch):
    """A start from a box without a number used to do nothing at all, without a word."""
    saved = []
    monkeypatch.setattr(GuiSettings, "save", lambda settings, path=None: saved.append(settings))
    make_manager = MagicMock()
    make_manager.return_value.ordered_jobs = []
    monkeypatch.setattr("bdfr.gui.JobManager", make_manager)
    _type_over(window.spin_concurrency, "")
    _type_over(window.spin_max_parallel, "40")

    window.button_start.invoke()

    make_manager.assert_called_once_with(max_parallel=MAX_PARALLEL_JOBS)
    manager = make_manager.return_value
    assert [added.args[0] for added in manager.add_job.call_args_list] == ["alice", "bob"]
    assert {added.args[1].concurrency for added in manager.add_job.call_args_list} == {DEFAULT_CONCURRENCY}
    (settings,) = saved
    assert (settings.concurrency, settings.max_parallel_jobs) == (DEFAULT_CONCURRENCY, MAX_PARALLEL_JOBS)
    # The boxes steer the downloads from here on, so they show what the downloads were started with ...
    assert window.spin_concurrency.get() == str(DEFAULT_CONCURRENCY)
    assert window.spin_max_parallel.get() == str(MAX_PARALLEL_JOBS)
    # ... and that is what stands in from now on for a box without a number.
    _type_over(window.spin_max_parallel, "")
    _enter(window.spin_max_parallel)
    assert window.spin_max_parallel.get() == str(MAX_PARALLEL_JOBS)
    # The downloads were started with it, so there is nothing to tell them.
    assert _numbers_passed_on(manager.set_max_parallel) == []
    _click_arrow(window.spin_max_parallel, "downarrow")
    assert _numbers_passed_on(manager.set_max_parallel) == [MAX_PARALLEL_JOBS - 1]


@pytest.mark.gui
def test_start_reads_the_numbers_as_they_are_written(window, monkeypatch: pytest.MonkeyPatch):
    """Tcl, which the boxes used to be read through, takes 010 for the octal of 8 and 08 for no number at all."""
    monkeypatch.setattr(GuiSettings, "save", lambda settings, path=None: None)
    make_manager = MagicMock()
    make_manager.return_value.ordered_jobs = []
    monkeypatch.setattr("bdfr.gui.JobManager", make_manager)
    _type_over(window.spin_max_parallel, "010")
    _type_over(window.spin_concurrency, "08")

    window.button_start.invoke()

    make_manager.assert_called_once_with(max_parallel=10)
    manager = make_manager.return_value
    assert {added.args[1].concurrency for added in manager.add_job.call_args_list} == {8}
    assert (window.spin_max_parallel.get(), window.spin_concurrency.get()) == ("10", "8")


@pytest.mark.gui
def test_closing_saves_the_names_while_a_spinbox_holds_no_number(
    tmp_path: Path, clipboard: _FakeClipboard, monkeypatch: pytest.MonkeyPatch
):
    saved = []
    monkeypatch.setattr(GuiSettings, "save", lambda settings, path=None: saved.append(settings))
    root = _new_tk_root()

    from bdfr.gui import BdfrGui

    closed = False
    try:
        # A settings file edited by hand can hold anything; the default stands in for what is no number.
        opened_with = GuiSettings(usernames="alice", directory=str(tmp_path), concurrency=7, max_parallel_jobs="many")
        gui = BdfrGui(root, opened_with)
        assert (gui.spin_concurrency.get(), gui.spin_max_parallel.get()) == ("7", str(DEFAULT_MAX_PARALLEL_JOBS))
        gui.text_usernames.insert("end", "\nbob")
        _type_over(gui.spin_concurrency, "")
        gui.on_close()
        closed = True
    finally:
        if not closed:
            root.destroy()
    (settings,) = saved
    assert parse_usernames(settings.usernames) == ["alice", "bob"]
    assert (settings.concurrency, settings.max_parallel_jobs) == (7, DEFAULT_MAX_PARALLEL_JOBS)


@pytest.mark.gui
def test_window_opens_with_numbers_inside_the_spinbox_ranges(tmp_path: Path, clipboard: _FakeClipboard):
    """What a settings file from before the ranges existed, or one edited by hand, can hold."""
    root = _new_tk_root()

    from bdfr.gui import BdfrGui

    try:
        gui = BdfrGui(
            root, GuiSettings(usernames="alice", directory=str(tmp_path), concurrency=500, max_parallel_jobs=0)
        )
        assert (gui.spin_concurrency.get(), gui.spin_max_parallel.get()) == (str(MAX_CONCURRENCY), "1")
        collected = gui.collect_settings()
        assert (collected.concurrency, collected.max_parallel_jobs) == (MAX_CONCURRENCY, 1)
    finally:
        root.destroy()
