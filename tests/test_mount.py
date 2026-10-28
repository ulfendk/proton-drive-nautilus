"""Mounts pdfs for real, backed by tests/fake_proton_drive.py."""

import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

FAKE_CLI = os.path.join(os.path.dirname(__file__), "fake_proton_drive.py")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    not os.path.exists("/dev/fuse") or not shutil.which("fusermount3"),
    reason="needs FUSE",
)


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def read(path):
    with open(path, "rb") as f:
        return f.read()


def write(path, data, mode="wb"):
    with open(path, mode) as f:
        f.write(data)


class Mount:
    def __init__(self, tmp_path, env=None, args=()):
        self.tmp_path = tmp_path
        self.env = env or {}
        self.args = list(args)
        self.backend = tmp_path / "backend"
        self.files = self.backend / "my-files"
        self.mountpoint = tmp_path / "mnt"
        self.state = tmp_path / "state"
        self.files.mkdir(parents=True)
        (self.files / "Documents").mkdir()
        write(self.files / "Documents" / "notes.txt", b"hello from proton\n")
        write(self.files / "top.bin", os.urandom(300_000))
        self.start()

    def start(self):
        tmp_path = self.tmp_path
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pdfs",
                str(self.mountpoint),
                "--cli",
                FAKE_CLI,
                "--cache-dir",
                str(tmp_path / "cache"),
                "--state-dir",
                str(self.state),
                *self.args,
            ],
            cwd=REPO,
            env={**os.environ, **self.env, "FAKE_DRIVE_ROOT": str(self.backend)},
        )
        wait_for(lambda: os.path.ismount(self.mountpoint))

    def path(self, *parts):
        return os.path.join(self.mountpoint, *parts)

    def calls(self, command):
        with open(self.backend / "calls.log") as f:
            return [call for call in map(json.loads, f) if call[1] == command]

    def status(self):
        with open(self.state / "status.json") as f:
            return json.load(f)

    def wait_uploaded(self):
        wait_for(lambda: not self.status()["status"])

    def stop(self):
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=30)
        if os.path.ismount(self.mountpoint):
            subprocess.run(["fusermount3", "-uz", str(self.mountpoint)])


@pytest.fixture
def mount(tmp_path):
    m = Mount(tmp_path)
    yield m
    m.stop()
    assert m.process.returncode == 0


def test_lists_and_reads(mount):
    assert sorted(os.listdir(mount.path())) == ["Documents", "top.bin"]
    assert os.path.isdir(mount.path("Documents"))
    assert read(mount.path("Documents", "notes.txt")) == b"hello from proton\n"
    assert read(mount.path("top.bin")) == read(mount.files / "top.bin")
    assert os.path.getsize(mount.path("top.bin")) == 300_000


def test_reads_come_from_cache(mount):
    read(mount.path("top.bin"))
    read(mount.path("top.bin"))
    assert len(mount.calls("download")) == 1


def test_missing_file(mount):
    with pytest.raises(FileNotFoundError):
        os.stat(mount.path("nope.txt"))


def test_creates_file(mount):
    write(mount.path("Documents", "new.txt"), b"fresh")
    assert os.path.getsize(mount.path("Documents", "new.txt")) == 5
    mount.wait_uploaded()
    assert read(mount.files / "Documents" / "new.txt") == b"fresh"


def test_copies_large_file(mount, tmp_path):
    source = tmp_path / "big.bin"
    write(source, os.urandom(5_000_000))
    shutil.copy(source, mount.path("big.bin"))
    mount.wait_uploaded()
    assert read(mount.files / "big.bin") == read(source)


def test_overwrites_and_appends(mount):
    write(mount.path("Documents", "notes.txt"), b"replaced")
    mount.wait_uploaded()
    assert read(mount.files / "Documents" / "notes.txt") == b"replaced"
    write(mount.path("Documents", "notes.txt"), b" more", mode="ab")
    mount.wait_uploaded()
    assert read(mount.files / "Documents" / "notes.txt") == b"replaced more"


def test_reading_without_writing_does_not_upload(mount):
    with open(mount.path("Documents", "notes.txt"), "r+b") as f:
        f.read()
    mount.wait_uploaded()
    assert mount.calls("upload") == []


def test_mkdir_rename_move_delete(mount):
    os.mkdir(mount.path("Archive"))
    assert (mount.files / "Archive").is_dir()

    os.rename(mount.path("Documents", "notes.txt"), mount.path("Documents", "renamed.txt"))
    assert (mount.files / "Documents" / "renamed.txt").exists()

    os.rename(mount.path("Documents", "renamed.txt"), mount.path("Archive", "moved.txt"))
    assert read(mount.files / "Archive" / "moved.txt") == b"hello from proton\n"
    assert read(mount.path("Archive", "moved.txt")) == b"hello from proton\n"

    with pytest.raises(OSError) as error:
        os.rmdir(mount.path("Archive"))
    assert error.value.errno == errno.ENOTEMPTY

    os.unlink(mount.path("Archive", "moved.txt"))
    os.rmdir(mount.path("Archive"))
    assert not (mount.files / "Archive").exists()
    assert len(os.listdir(mount.backend / "trash")) == 2
    assert sorted(os.listdir(mount.path())) == ["Documents", "top.bin"]


def test_rename_replaces_existing(mount):
    write(mount.path("Documents", "other.txt"), b"other")
    mount.wait_uploaded()
    os.replace(mount.path("Documents", "other.txt"), mount.path("Documents", "notes.txt"))
    assert read(mount.files / "Documents" / "notes.txt") == b"other"
    assert os.listdir(mount.path("Documents")) == ["notes.txt"]


def test_editor_style_save(mount):
    # Write a temp file, rename it over the original while it's still open.
    f = open(mount.path("Documents", ".notes.txt.tmp"), "wb")
    f.write(b"saved by editor")
    os.replace(mount.path("Documents", ".notes.txt.tmp"), mount.path("Documents", "notes.txt"))
    f.close()
    mount.wait_uploaded()
    assert read(mount.files / "Documents" / "notes.txt") == b"saved by editor"
    assert os.listdir(mount.files / "Documents") == ["notes.txt"]


def test_refuses_trash_folder(mount):
    with pytest.raises(PermissionError):
        os.mkdir(mount.path(".Trash-1000"))


def test_status_xattr(mount):
    assert os.getxattr(mount.path("top.bin"), "user.protondrive.status") == b"synced"


def test_reports_transfer_progress(tmp_path):
    mount = Mount(tmp_path, env={"FAKE_DRIVE_SLOW": "1"})
    try:
        write(mount.path("slow.bin"), os.urandom(100_000))
        seen = []

        def halfway():
            transfers = mount.status()["transfers"]
            seen.extend(transfers)
            return any(0 < t["done"] < t["total"] for t in transfers)

        wait_for(halfway)
        assert seen[-1]["path"] == "slow.bin"
        assert seen[-1]["direction"] == "up"
        assert seen[-1]["total"] == 100_000
        mount.wait_uploaded()
        wait_for(lambda: not mount.status()["transfers"])
    finally:
        mount.stop()


def test_listings_survive_restart(mount):
    os.listdir(mount.path("Documents"))
    listed = len(mount.calls("list"))
    mount.stop()
    mount.start()
    assert os.listdir(mount.path("Documents")) == ["notes.txt"]
    assert len(mount.calls("list")) == listed


def test_stale_listing_is_served_then_refreshed(mount):
    assert os.listdir(mount.path("Documents")) == ["notes.txt"]
    write(mount.files / "Documents" / "added-elsewhere.txt", b"remote change")
    time.sleep(5.5)  # Past the reopen threshold.
    # Answered from cache immediately; the refresh runs in the background.
    assert os.listdir(mount.path("Documents")) == ["notes.txt"]
    wait_for(lambda: "added-elsewhere.txt" in os.listdir(mount.path("Documents")))


def test_hide_from_indexers(tmp_path):
    mount = Mount(tmp_path, args=["--hide-from-indexers"])
    try:
        check_virtual_trackerignore(mount)
    finally:
        mount.stop()


def check_virtual_trackerignore(mount):
    assert ".trackerignore" in os.listdir(mount.path())
    assert read(mount.path(".trackerignore")) == b""
    with pytest.raises(PermissionError):
        os.unlink(mount.path(".trackerignore"))
    with pytest.raises(PermissionError):
        write(mount.path(".trackerignore"), b"x")
    assert not (mount.files / ".trackerignore").exists()
    assert mount.calls("download") == []


def test_refuses_indexers(mount, tmp_path):
    indexer = tmp_path / "localsearch-3"
    shutil.copy(shutil.which("cat"), indexer)
    result = subprocess.run([indexer, mount.path("top.bin")], capture_output=True)
    assert result.returncode != 0
    assert b"Permission denied" in result.stderr
    assert mount.calls("download") == []
    # Other programs still read normally.
    assert read(mount.path("top.bin")) == read(mount.files / "top.bin")


def test_indexed_by_default(mount):
    assert ".trackerignore" not in os.listdir(mount.path())


def test_clear_cache(mount):
    assert read(mount.path("top.bin")) == read(mount.files / "top.bin")
    assert mount.status()["cacheBytes"] == 300_000
    write(mount.path("pending.txt"), b"not uploaded yet")
    os.setxattr(mount.path(), "user.protondrive.clear-cache", b"1")
    assert mount.status()["cacheBytes"] == 0
    # Downloads again on the next open.
    assert read(mount.path("top.bin")) == read(mount.files / "top.bin")
    assert len(mount.calls("download")) == 2
    mount.wait_uploaded()
    assert read(mount.files / "pending.txt") == b"not uploaded yet"
