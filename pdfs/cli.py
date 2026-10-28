"""Async wrapper around the official `proton-drive` CLI.

This is the only module that runs the CLI. Every call is a one-shot process
with `--json`; results are parsed into `Node` values and failures are raised
as `CliError` carrying the errno the filesystem should report.
"""

import errno
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from functools import partial
from datetime import datetime

import trio

log = logging.getLogger(__name__)

# The CLI's name for the top of the user's own Drive.
REMOTE_ROOT = "/my-files"


class CliError(Exception):
    def __init__(self, message, err=errno.EIO):
        super().__init__(message)
        self.errno = err


@dataclass(frozen=True)
class Node:
    uid: str | None
    name: str
    is_dir: bool
    size: int
    mtime: float
    ctime: float
    revision: str | None = None
    media_type: str | None = None


def _timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_node(raw):
    """Turns a serialized SDK NodeEntity into a Node, or None if it can't be shown."""
    if raw.get("type") not in ("file", "folder"):
        return None
    name = raw.get("name") or {}
    if not name.get("ok") or not name.get("value"):
        # Names that failed to decrypt can't be addressed by path.
        return None

    is_dir = raw["type"] == "folder"
    revision = raw.get("activeRevision") or {}
    folder = raw.get("folder") or {}
    if is_dir:
        mtime = folder.get("claimedModificationTime") or raw.get("modificationTime")
        size = 0
    else:
        mtime = revision.get("claimedModificationTime") or raw.get("modificationTime")
        size = revision.get("claimedSize")
        if size is None:
            size = revision.get("storageSize", 0)

    return Node(
        uid=raw.get("uid"),
        name=name["value"],
        is_dir=is_dir,
        size=int(size),
        mtime=_timestamp(mtime),
        ctime=_timestamp(raw.get("creationTime")),
        revision=revision.get("uid"),
        media_type=raw.get("mediaType"),
    )


def errno_for(message):
    lowered = message.lower()
    if "not found" in lowered:
        return errno.ENOENT
    if "login" in lowered or "not authenticated" in lowered:
        return errno.EACCES
    if "already exists" in lowered or "same name" in lowered:
        return errno.EEXIST
    if "quota" in lowered or "storage" in lowered and "full" in lowered:
        return errno.ENOSPC
    return errno.EIO


# The CLI draws "⠋ 42.17% name (9.65 MiB)" for each active transfer.
PROGRESS = re.compile(rb"(\d+(?:\.\d+)?)% ")


def _check_results(stdout, action):
    """Commands acting on several nodes print [{uid, ok, error}, ...]."""
    for result in json.loads(stdout):
        if not result.get("ok"):
            raise CliError(f"{action} failed for {result.get('uid')}")


class Cli:
    def __init__(self, binary="proton-drive", max_concurrent=3):
        self.binary = binary
        # None until the first call; the bar widget shows a login button when False.
        self.logged_in = None
        self.on_login_change = None
        # Parallel CLI processes share its sqlite caches, so keep this low.
        self._limit = trio.Semaphore(max_concurrent)

    async def _run(self, group, command, *args, options=(), on_progress=None):
        """Runs one CLI command.

        With `on_progress`, the command runs without --json on a pseudo
        terminal, the only place the CLI reports progress, and
        on_progress(fraction) is called as it changes. Its stdout is then
        not returned.
        """
        flags = [] if on_progress else ["--json"]
        cmd = [self.binary, group, command, *flags, *options, "--", *args]
        async with self._limit:
            log.debug("running %s", cmd)
            if on_progress:
                proc = await self._run_with_progress(cmd, on_progress)
            else:
                proc = await trio.run_process(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    capture_stdout=True,
                    capture_stderr=True,
                    check=False,
                )
        if proc.returncode != 0:
            message = proc.stderr.decode(errors="replace").strip()
            message = message.splitlines()[-1] if message else f"exit code {proc.returncode}"
            log.warning("%s %s failed: %s", command, args, message)
            error = CliError(message, errno_for(message))
            if error.errno == errno.EACCES:
                self._set_logged_in(False)
            raise error
        self._set_logged_in(True)
        return proc.stdout.decode() if proc.stdout else ""

    async def _run_with_progress(self, cmd, on_progress):
        master, slave = os.openpty()
        with tempfile.TemporaryFile() as err:
            try:
                async with trio.open_nursery() as nursery:
                    run = partial(trio.run_process, cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=err, check=False)
                    proc = await nursery.start(run)
                    os.close(slave)
                    slave = None
                    await self._read_progress(master, on_progress)
            finally:
                if slave is not None:
                    os.close(slave)
            err.seek(0)
            return subprocess.CompletedProcess(cmd, proc.returncode, b"", err.read())

    async def _read_progress(self, master, on_progress):
        stream = trio.lowlevel.FdStream(master)  # Takes ownership of `master`.
        async with stream:
            while True:
                try:
                    chunk = await stream.receive_some(65536)
                except trio.BrokenResourceError:
                    break  # EIO: the process closed the terminal.
                if not chunk:
                    break
                matches = PROGRESS.findall(chunk)
                if matches:
                    on_progress(min(float(matches[-1]) / 100, 1.0))

    def _set_logged_in(self, value):
        if self.logged_in != value:
            self.logged_in = value
            if self.on_login_change:
                self.on_login_change()

    async def list(self, remote_dir):
        stdout = await self._run("filesystem", "list", remote_dir)
        nodes = (parse_node(raw) for raw in json.loads(stdout))
        return [node for node in nodes if node is not None]

    async def info(self, remote_path):
        node = parse_node(json.loads(await self._run("filesystem", "info", remote_path)))
        if node is None:
            raise CliError(f"unsupported node: {remote_path}")
        return node

    async def create_folder(self, remote_parent, name):
        node = parse_node(json.loads(await self._run("filesystem", "create-folder", remote_parent, name)))
        if node is None:
            raise CliError(f"unexpected create-folder output for {name}")
        return node

    async def rename(self, remote_path, new_name):
        await self._run("filesystem", "rename", remote_path, new_name)

    async def move(self, remote_path, remote_target_parent):
        _check_results(await self._run("filesystem", "move", remote_path, remote_target_parent), "move")

    async def trash(self, remote_path):
        _check_results(await self._run("filesystem", "trash", remote_path), "trash")

    async def download(self, remote_path, local_dir, on_progress=None):
        await self._run("filesystem", "download", remote_path, local_dir, on_progress=on_progress)

    async def upload(self, local_path, remote_parent, on_progress=None):
        # create-new-revision keeps Proton's version history when overwriting.
        await self._run(
            "filesystem",
            "upload",
            local_path,
            remote_parent,
            options=["--file-conflict-strategy", "create-new-revision"],
            on_progress=on_progress,
        )
