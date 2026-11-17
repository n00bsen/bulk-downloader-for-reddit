#!/usr/bin/env python3

import configparser
import sys
from pathlib import Path

import praw
import pytest

from bdfr.connector import build_user_agent
from bdfr.oauth2 import OAuth2TokenManager


@pytest.fixture(scope="session")
def reddit_instance():
    rd = praw.Reddit(
        client_id="U-6gk4ZCh3IeNQ",
        client_secret="7CZHY6AmKweZME5s50SfDGylaPg",
        user_agent=build_user_agent(),
        check_for_updates=False,
    )
    return rd


@pytest.fixture(scope="session")
def authenticated_reddit_instance():
    test_config_path = Path("./tests/test_config.cfg")
    if not test_config_path.exists():
        pytest.skip("Refresh token must be provided to authenticate with OAuth2")
    cfg_parser = configparser.ConfigParser()
    cfg_parser.read(test_config_path)
    if not cfg_parser.has_option("DEFAULT", "user_token"):
        pytest.skip("Refresh token must be provided to authenticate with OAuth2")
    token_manager = OAuth2TokenManager(cfg_parser, test_config_path)
    reddit_instance = praw.Reddit(
        client_id=cfg_parser.get("DEFAULT", "client_id"),
        client_secret=cfg_parser.get("DEFAULT", "client_secret"),
        # Never the hostname: this is sent to Reddit.
        user_agent=build_user_agent(),
        check_for_updates=False,
        token_manager=token_manager,
    )
    return reddit_instance


@pytest.fixture(scope="session", autouse=True)
def no_visible_windows():
    """Keep the GUI tests from flashing windows on the screen of whoever runs them.

    Every Tk window is made fully transparent, and kept off the taskbar, the
    moment it is created and before the event loop can draw it. It still has
    to be a real, mapped window: Tk only tracks focus and mouse positions for
    those, so simply withdrawing it would break the tests that click and
    paste. Pop-up menus and dialogs are blocked: a real one would also wait
    for a person.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except ImportError:
        yield
        return

    patch = pytest.MonkeyPatch()

    def invisible_after_init(cls):
        original_init = cls.__init__

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            try:
                self.attributes("-alpha", 0.0)
                if sys.platform == "win32":
                    self.attributes("-toolwindow", True)
            except tk.TclError:
                # Not every window system supports these; the tests still run.
                pass

        patch.setattr(cls, "__init__", init)

    def blocked(name):
        def dialog(*args, **kwargs):
            raise AssertionError(f"{name} would open a dialog during a test; stub it")

        return dialog

    invisible_after_init(tk.Tk)
    invisible_after_init(tk.Toplevel)
    patch.setattr(tk.Menu, "tk_popup", lambda self, *args, **kwargs: None)
    for name in ("showinfo", "showwarning", "showerror", "askokcancel", "askyesno", "askyesnocancel"):
        patch.setattr(messagebox, name, blocked(f"messagebox.{name}"))
    patch.setattr(filedialog, "askdirectory", blocked("filedialog.askdirectory"))
    yield
    patch.undo()
