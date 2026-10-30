"""Mounts protondrivefs for real, backed by tests/fake_proton_drive.py."""

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
FAKE_WORKER = os.path.join(os.path.dirname(__file__), "fake_proton_drive_worker.py")
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
    def __init__(self, tmp_path, env=None, args=(), worker=FAKE_WORKER):
        self.tmp_path = tmp_path
        self.env = env or {}
        self.args = list(args)
        self.worker = worker or ""
        self.backend = tmp_path / "backend"
        self.files = self.backend / "my-files"
        self.mountpoint = tmp_path / "mnt"
        self.state = tmp_path / "state"
        self.local = tmp_path / "local"
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
                "protondrivefs",
                str(self.mountpoint),
                "--cli",
                FAKE_CLI,
                "--worker",
                self.worker,
                "--cache-dir",
                str(tmp_path / "cache"),
                "--state-dir",
                str(self.state),
                "--local-dir",
                str(self.local),
                "--quiet-seconds",
                "0.3",
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

    def worker_calls(self):
        try:
            with open(self.backend / "worker.log") as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def status(self):
        with open(self.state / "status.json") as f:
            return json.load(f)

    def wait_uploaded(self):
        wait_for(lambda: all(state == "local" for state in self.status()["status"].values()))

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
    mount.wait_uploaded()
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


INDEXER_THREAD = """
import ctypes, sys, threading
def run():
    # Like localsearch-extractor's "pool-0" threads.
    ctypes.CDLL(None).prctl(15, b"pool-0")  # PR_SET_NAME
    try:
        open(sys.argv[1], "rb").read()
        print("read")
    except PermissionError:
        print("refused")
thread = threading.Thread(target=run)
thread.start()
thread.join()
"""


def test_refuses_indexer_worker_threads(mount, tmp_path):
    indexer = tmp_path / "localsearch-extractor-3"
    shutil.copy(os.path.realpath(sys.executable), indexer)
    result = subprocess.run([indexer, "-c", INDEXER_THREAD, mount.path("top.bin")], capture_output=True, check=True)
    assert result.stdout == b"refused\n"
    assert mount.calls("download") == []


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


def test_metadata_goes_through_worker(mount):
    os.listdir(mount.path("Documents"))
    os.mkdir(mount.path("New"))
    mount.wait_uploaded()
    os.rename(mount.path("New"), mount.path("Renamed"))
    assert read(mount.path("top.bin")) == read(mount.files / "top.bin")
    write(mount.path("up.txt"), b"up")
    mount.wait_uploaded()
    commands = {call["argv"][1] for call in mount.worker_calls()}
    assert {"list", "create-folder", "rename"} <= commands
    # Transfers report progress on a terminal, so they always run the CLI.
    assert not commands & {"upload", "download"}
    assert len({call["pid"] for call in mount.worker_calls()}) == 1


def test_works_without_worker(tmp_path):
    mount = Mount(tmp_path, worker=None)
    try:
        assert os.listdir(mount.path("Documents")) == ["notes.txt"]
        os.mkdir(mount.path("New"))
        assert mount.worker_calls() == []
    finally:
        mount.stop()


def test_falls_back_when_worker_never_answers(tmp_path):
    mount = Mount(tmp_path, env={"FAKE_WORKER_EXIT_AFTER": "0"})
    try:
        assert os.listdir(mount.path("Documents")) == ["notes.txt"]
        os.mkdir(mount.path("Later"))
        assert os.listdir(mount.path("Later")) == []
        # One try, then the worker isn't started again.
        assert len({call["pid"] for call in mount.worker_calls()}) == 1
    finally:
        mount.stop()


def test_restarts_worker_after_it_exits(tmp_path):
    mount = Mount(tmp_path, env={"FAKE_WORKER_EXIT_AFTER": "1"})
    try:
        for name in ("A", "B", "C"):
            (mount.files / name).mkdir()
            write(mount.files / name / "inside.txt", name.encode())
        # Each listing is answered, by the worker or by the CLI after it exits.
        for name in ("A", "B", "C"):
            assert os.listdir(mount.path(name)) == ["inside.txt"]
        assert len({call["pid"] for call in mount.worker_calls()}) >= 2
    finally:
        mount.stop()


def test_new_folders_upload_once_the_copy_pauses(mount):
    os.makedirs(mount.path("Empty", "Nested"))
    write(mount.path("Empty", "Nested", "file.txt"), b"inside")
    assert sorted(os.listdir(mount.path("Empty"))) == ["Nested"]
    mount.wait_uploaded()
    assert read(mount.files / "Empty" / "Nested" / "file.txt") == b"inside"
    os.mkdir(mount.path("Gone"))
    os.rmdir(mount.path("Gone"))
    os.mkdir(mount.path("Before"))
    os.rename(mount.path("Before"), mount.path("After"))
    mount.wait_uploaded()
    assert (mount.files / "After").is_dir()
    assert not (mount.files / "Gone").exists() and not (mount.files / "Before").exists()


def test_moving_into_a_new_folder_copies(mount):
    os.mkdir(mount.path("Fresh"))
    with pytest.raises(OSError) as error:
        os.rename(mount.path("top.bin"), mount.path("Fresh", "top.bin"))
    assert error.value.errno == errno.EXDEV
    shutil.move(mount.path("Documents", "notes.txt"), mount.path("Fresh", "notes.txt"))
    mount.wait_uploaded()
    assert read(mount.files / "Fresh" / "notes.txt") == b"hello from proton\n"


def copy_repository(root):
    # Like cp -r in readdir order: the folder and its files before .git.
    os.makedirs(os.path.join(root, "src"))
    write(os.path.join(root, "README.md"), b"# project\n")
    write(os.path.join(root, "src", "main.py"), b"print('hi')\n")
    os.makedirs(os.path.join(root, ".git", "objects"))
    write(os.path.join(root, ".git", "HEAD"), b"ref: refs/heads/main\n")


def test_keeps_git_repository_local(mount):
    os.mkdir(mount.path("Code"))
    write(mount.path("Code", "notes.txt"), b"not a repo")
    copy_repository(mount.path("Code", "project"))
    mount.wait_uploaded()

    assert read(mount.files / "Code" / "notes.txt") == b"not a repo"
    assert not (mount.files / "Code" / "project").exists()
    assert mount.status()["status"] == {"Code/project": "local"}
    assert os.getxattr(mount.path("Code", "project", "README.md"), "user.protondrive.status") == b"local"
    assert sorted(os.listdir(mount.path("Code"))) == ["notes.txt", "project"]
    assert sorted(os.listdir(mount.path("Code", "project"))) == [".git", "README.md", "src"]
    assert read(mount.path("Code", "project", "src", "main.py")) == b"print('hi')\n"

    # Edited in place, renamed within, and still there after a restart.
    write(mount.path("Code", "project", "README.md"), b"# changed\n")
    os.rename(mount.path("Code", "project", "src"), mount.path("Code", "project", "lib"))
    mount.stop()
    mount.start()
    assert read(mount.path("Code", "project", "README.md")) == b"# changed\n"
    assert os.listdir(mount.path("Code", "project", "lib")) == ["main.py"]
    mount.wait_uploaded()
    assert not [call for call in mount.calls("upload") if "project" in json.dumps(call)]

    # Moving files in or out copies them instead.
    with pytest.raises(OSError) as error:
        os.rename(mount.path("Code", "notes.txt"), mount.path("Code", "project", "notes.txt"))
    assert error.value.errno == errno.EXDEV

    # The repository moves with its folder, and is gone once deleted.
    os.rename(mount.path("Code"), mount.path("Projects"))
    assert read(mount.path("Projects", "project", "README.md")) == b"# changed\n"
    shutil.rmtree(mount.path("Projects", "project"))
    assert os.listdir(mount.path("Projects")) == ["notes.txt"]
    assert mount.status()["status"] == {}
    assert not os.listdir(mount.local / "files")


def test_git_init_in_folder_on_the_drive_keeps_only_git_local(mount):
    os.mkdir(mount.path("Documents", ".git"))
    write(mount.path("Documents", ".git", "HEAD"), b"ref: refs/heads/main\n")
    write(mount.path("Documents", "tracked.txt"), b"tracked")
    mount.wait_uploaded()
    assert read(mount.files / "Documents" / "tracked.txt") == b"tracked"
    assert not (mount.files / "Documents" / ".git").exists()
    assert sorted(os.listdir(mount.path("Documents"))) == [".git", "notes.txt", "tracked.txt"]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_git_clone_into_the_mount(mount, tmp_path):
    source = tmp_path / "source"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", str(source)], check=True)
    write(source / "file.txt", b"committed")
    subprocess.run([*git, "-C", str(source), "add", "."], check=True)
    subprocess.run([*git, "-C", str(source), "commit", "-qm", "first"], check=True)

    subprocess.run([*git, "clone", "-q", str(source), mount.path("clone")], check=True)
    subprocess.run([*git, "-C", mount.path("clone"), "status", "--short"], check=True)
    write(mount.path("clone", "file.txt"), b"edited")
    subprocess.run([*git, "-C", mount.path("clone"), "commit", "-qam", "second"], check=True)
    log = subprocess.run([*git, "-C", mount.path("clone"), "log", "--oneline"], capture_output=True, check=True)
    assert len(log.stdout.splitlines()) == 2
    mount.wait_uploaded()
    assert not (mount.files / "clone").exists()
    assert mount.calls("upload") == []


def test_upload_git_repos(tmp_path):
    mount = Mount(tmp_path, args=["--upload-git-repos"])
    try:
        copy_repository(mount.path("project"))
        mount.wait_uploaded()
        assert read(mount.files / "project" / ".git" / "HEAD") == b"ref: refs/heads/main\n"
        assert read(mount.files / "project" / "src" / "main.py") == b"print('hi')\n"
    finally:
        mount.stop()
