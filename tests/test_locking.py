#!/usr/bin/env python3

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from bdfr.locking import LockTimeoutError, atomic_write, atomic_write_bytes, file_lock


def test_atomic_write_creates_file(tmp_path: Path):
    target = tmp_path / "out.txt"
    atomic_write(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_atomic_write_replaces_existing_content(tmp_path: Path):
    target = tmp_path / "out.txt"
    target.write_text("old content that is much longer", encoding="utf-8")
    atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_creates_parent_directories(tmp_path: Path):
    target = tmp_path / "a" / "b" / "out.txt"
    atomic_write(target, "nested")
    assert target.read_text(encoding="utf-8") == "nested"


def test_atomic_write_leaves_no_temp_files(tmp_path: Path):
    target = tmp_path / "out.txt"
    atomic_write(target, "hello")
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_preserves_original_on_failure(tmp_path: Path):
    target = tmp_path / "out.txt"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(TypeError):
        atomic_write(target, None)  # type: ignore[arg-type]
    assert target.read_text(encoding="utf-8") == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_bytes_roundtrip(tmp_path: Path):
    target = tmp_path / "out.bin"
    payload = bytes(range(256))
    atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload


def test_file_lock_is_reentrant_across_sequential_uses(tmp_path: Path):
    target = tmp_path / "cfg.ini"
    for _ in range(3):
        with file_lock(target):
            atomic_write(target, "value")
    assert target.read_text(encoding="utf-8") == "value"


def test_file_lock_creates_sidecar_not_target(tmp_path: Path):
    target = tmp_path / "cfg.ini"
    with file_lock(target):
        assert (tmp_path / "cfg.ini.lock").exists()
        assert not target.exists()


def test_file_lock_blocks_other_process(tmp_path: Path):
    """A second process must not be able to take a held lock."""
    target = tmp_path / "cfg.ini"
    probe = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path.cwd())!r})
        from pathlib import Path
        from bdfr.locking import LockTimeoutError, file_lock
        try:
            with file_lock(Path({str(target)!r}), timeout=0.5):
                print("ACQUIRED")
        except LockTimeoutError:
            print("BLOCKED")
        """
    )
    with file_lock(target):
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert result.stdout.strip() == "BLOCKED", result.stderr


def test_file_lock_released_after_block(tmp_path: Path):
    """Once released, another process can take the lock."""
    target = tmp_path / "cfg.ini"
    with file_lock(target):
        pass
    probe = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path.cwd())!r})
        from pathlib import Path
        from bdfr.locking import file_lock
        with file_lock(Path({str(target)!r}), timeout=5):
            print("ACQUIRED")
        """
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == "ACQUIRED", result.stderr


def test_lock_timeout_raises(tmp_path: Path):
    target = tmp_path / "cfg.ini"
    probe = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path.cwd())!r})
        from pathlib import Path
        from bdfr.locking import file_lock
        with file_lock(Path({str(target)!r})):
            print("HELD", flush=True)
            time.sleep(10)
        """
    )
    holder = subprocess.Popen([sys.executable, "-c", probe], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "HELD"
        with pytest.raises(LockTimeoutError):
            with file_lock(target, timeout=0.5):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_lock_survives_holder_death(tmp_path: Path):
    """An OS-level lock must be released when the holding process dies."""
    target = tmp_path / "cfg.ini"
    probe = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path.cwd())!r})
        from pathlib import Path
        from bdfr.locking import file_lock
        with file_lock(Path({str(target)!r})):
            print("HELD", flush=True)
            time.sleep(30)
        """
    )
    holder = subprocess.Popen([sys.executable, "-c", probe], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "HELD"
    holder.kill()
    holder.wait(timeout=30)
    # The dead process must not keep the lock held.
    with file_lock(target, timeout=10):
        atomic_write(target, "after death")
    assert target.read_text(encoding="utf-8") == "after death"


def test_concurrent_writers_never_produce_partial_file(tmp_path: Path):
    """Many processes writing the same config must always leave it fully valid."""
    target = tmp_path / "cfg.ini"
    expected = "x" * 5000
    writer = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path.cwd())!r})
        from pathlib import Path
        from bdfr.locking import atomic_write, file_lock
        target = Path({str(target)!r})
        for _ in range(20):
            with file_lock(target, timeout=60):
                atomic_write(target, {expected!r})
                assert target.read_text(encoding="utf-8") == {expected!r}
        print("OK")
        """
    )
    procs = [subprocess.Popen([sys.executable, "-c", writer], stdout=subprocess.PIPE, text=True) for _ in range(4)]
    outs = [p.communicate(timeout=180)[0].strip() for p in procs]
    assert all(o == "OK" for o in outs), outs
    assert target.read_text(encoding="utf-8") == expected
    # no temp or partial files left behind
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == ["cfg.ini", "cfg.ini.lock"], leftovers


def test_atomic_write_uses_pid_scoped_temp_name(tmp_path: Path):
    """Concurrent writers must not collide on the same temp filename."""
    target = tmp_path / "out.txt"
    atomic_write(target, "content")
    assert str(os.getpid()) not in [p.name for p in tmp_path.iterdir()]
