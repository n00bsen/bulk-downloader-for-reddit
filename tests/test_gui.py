#!/usr/bin/env python3

"""Tests for the GUI layer.

Only the pure logic is covered here -- settings round-tripping, input parsing
and the translation from window state into per-job Configuration objects. That
is where the behaviour that used to live in the batch file now lives, so it is
the part worth pinning down. Widget construction needs a display and is checked
separately by the `gui` marked test.
"""

import json
from pathlib import Path

import pytest

from bdfr.gui import (
    MIN_JOB_TABLE_HEIGHT,
    SCREEN_HEIGHT_ALLOWANCE,
    GuiSettings,
    build_job_configs,
    fit_height_to_screen,
    parse_skip_list,
    parse_usernames,
    validate_settings,
)


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
