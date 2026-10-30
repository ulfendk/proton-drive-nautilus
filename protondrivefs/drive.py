"""Path-level view of Proton Drive: listing cache, content cache and uploads.

Paths here are relative POSIX paths inside the mount ("" is the root,
"Documents/a.txt" a file). They map to CLI paths under /my-files.
"""

import errno
import hashlib
import json
import logging
import os
import posixpath
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace

import trio

from .cli import REMOTE_ROOT, CliError, Node

log = logging.getLogger(__name__)

ROOT_NODE = Node(uid=None, name="", is_dir=True, size=0, mtime=0.0, ctime=0.0)

# With hide_from_indexers, shown in the mount root but never stored in the
# Drive: LocalSearch (Tracker) skips folders containing .trackerignore. By
# default the Drive is indexed by name and metadata, and fs.py refuses
# indexers' content reads so indexing never downloads anything.
TRACKER_IGNORE = ".trackerignore"
TRACKER_IGNORE_NODE = Node(uid=None, name=TRACKER_IGNORE, is_dir=False, size=0, mtime=0.0, ctime=0.0)


def remote(path):
    return f"{REMOTE_ROOT}/{path}" if path else REMOTE_ROOT


def split(path):
    parent, name = posixpath.split(path)
    return parent, name


def is_within(path, ancestor):
    return path == ancestor or ancestor == "" or path.startswith(ancestor + "/")


@dataclass
class Pending:
    """A file whose newest content only exists locally, in `staging`."""

    staging: str
    state: str = "writing"  # writing -> queued -> uploading -> (gone) | error
    writers: int = 0
    dirty: bool = False
    error: str | None = None
    done: trio.Event = field(default_factory=trio.Event)


class Drive:
    def __init__(
        self, cli, cache_dir, list_ttl=30.0, cache_limit=5 * 1024**3, status_file=None, hide_from_indexers=False
    ):
        self.cli = cli
        self.hide_from_indexers = hide_from_indexers
        self.list_ttl = list_ttl
        self.cache_limit = cache_limit
        self.status_file = status_file
        self._content_dir = os.path.join(cache_dir, "content")
        self._staging_dir = os.path.join(cache_dir, "staging")
        # Staged files from a previous run may be partial writes; never upload them.
        shutil.rmtree(self._staging_dir, ignore_errors=True)
        os.makedirs(self._content_dir, exist_ok=True)
        os.makedirs(self._staging_dir, exist_ok=True)
        self._empty_file = os.path.join(cache_dir, "empty")
        open(self._empty_file, "wb").close()
        self.cache_bytes = 0

        # dir path -> (fetched_at, {name: Node}), kept across restarts so
        # browsing is instant from the start; it's refreshed in the background.
        self._listings_file = os.path.join(cache_dir, "listings.json")
        self._listings_dirty = False
        self._load_listings()
        self._refreshing = set()
        self._nursery = None
        self._transfers = {}  # id -> {"path", "direction", "done", "total"}
        self._next_transfer = 0
        self._locks = {}  # key -> trio.Lock, so concurrent requests share one CLI call
        self._pending = {}  # file path -> Pending
        self._send_upload, self._receive_upload = trio.open_memory_channel(1000)
        cli.on_login_change = self._write_status
        self._write_status()
        self._evict()

    def _lock(self, key):
        return self._locks.setdefault(key, trio.Lock())

    # Listings
    #
    # A CLI call takes about a second, so a folder that has been listed once
    # is always answered from memory. Listings older than `max_age` are
    # refreshed in the background; the next look (F5 in Nautilus) sees it.

    async def listdir(self, path, max_age=None):
        cached = self._listings.get(path)
        if cached is None:
            await self.refresh(path)
            cached = self._listings[path]
        elif time.time() - cached[0] > (self.list_ttl if max_age is None else max_age):
            self._refresh_soon(path)
        entries = dict(cached[1])
        if path == "" and self.hide_from_indexers:
            entries.setdefault(TRACKER_IGNORE, TRACKER_IGNORE_NODE)
        for pending_path, pending in self._pending.items():
            parent, name = split(pending_path)
            if parent == path:
                entries[name] = self._pending_node(name, pending, entries.get(name))
        return entries

    def _pending_node(self, name, pending, existing):
        st = os.stat(pending.staging)
        return Node(
            uid=existing.uid if existing else None,
            name=name,
            is_dir=False,
            size=st.st_size,
            mtime=st.st_mtime,
            ctime=existing.ctime if existing else st.st_mtime,
        )

    async def stat(self, path):
        if path == "":
            return ROOT_NODE
        parent, name = split(path)
        node = (await self.listdir(parent)).get(name)
        if node is None:
            raise CliError(f"not found: {path}", errno.ENOENT)
        return node

    async def refresh(self, path):
        """Lists `path` from the server, waiting for the result."""
        requested = time.time()
        async with self._lock(("list", path)):
            cached = self._listings.get(path)
            if cached and cached[0] >= requested:
                return  # Another caller refreshed it while we waited.
            nodes = await self.cli.list(remote(path))
            self._listings[path] = (time.time(), {node.name: node for node in nodes})
            self._listings_dirty = True

    def _refresh_soon(self, path):
        if self._nursery is None or path in self._refreshing:
            return
        self._refreshing.add(path)
        self._nursery.start_soon(self._background_refresh, path)

    async def _background_refresh(self, path):
        try:
            await self.refresh(path)
        except CliError as error:
            if error.errno == errno.ENOENT:
                self.invalidate(path)
        finally:
            self._refreshing.discard(path)

    def invalidate(self, path):
        """Drops cached listings for `path` and everything below it."""
        for cached in [p for p in self._listings if is_within(p, path)]:
            del self._listings[cached]
        self._listings_dirty = True

    def mark_stale(self, path):
        """Keeps serving the listing of `path`, but refreshes it in the background."""
        cached = self._listings.get(path)
        if cached:
            self._listings[path] = (0.0, cached[1])
            self._refresh_soon(path)

    def _update_cached(self, path, node):
        parent, name = split(path)
        cached = self._listings.get(parent)
        if cached is None:
            return
        if node is None:
            cached[1].pop(name, None)
        else:
            cached[1][name] = node
        self._listings_dirty = True

    def _load_listings(self):
        try:
            with open(self._listings_file) as f:
                saved = json.load(f)
            self._listings = {
                path: (fetched_at, {raw["name"]: Node(**raw) for raw in nodes})
                for path, (fetched_at, nodes) in saved.items()
            }
        except (OSError, ValueError, TypeError, KeyError):
            self._listings = {}

    def save_listings(self):
        if not self._listings_dirty:
            return
        saved = {path: (fetched_at, [asdict(n) for n in nodes.values()]) for path, (fetched_at, nodes) in self._listings.items()}
        tmp = f"{self._listings_file}.tmp"
        with open(tmp, "w") as f:
            json.dump(saved, f)
        os.replace(tmp, self._listings_file)
        self._listings_dirty = False

    async def run(self, upload_workers=2):
        """Background work: uploads, listing refreshes and saving listings."""
        async with trio.open_nursery() as nursery:
            self._nursery = nursery
            for _ in range(upload_workers):
                nursery.start_soon(self._upload_worker)
            while True:
                await trio.sleep(10)
                self.save_listings()

    # Reading

    async def fetch(self, path):
        """Returns a local file holding the current content of `path`."""
        pending = self._pending.get(path)
        if pending:
            return pending.staging
        if self._is_virtual(path):
            return self._empty_file

        node = await self.stat(path)
        if node.is_dir:
            raise CliError(f"is a directory: {path}", errno.EISDIR)
        if node.uid is None:
            # Just uploaded; the content key needs the server's node.
            await self.refresh(split(path)[0])
            node = await self.stat(path)
            if node.uid is None:
                raise CliError(f"{path} isn't on the server yet", errno.EAGAIN)

        key = hashlib.sha256(f"{node.uid}:{node.revision}".encode()).hexdigest()
        target = os.path.join(self._content_dir, key)
        async with self._lock(("fetch", key)):
            if os.path.exists(target):
                os.utime(target)
                return target
            with tempfile.TemporaryDirectory(dir=self._staging_dir) as tmp, self._transfer(path, "down", node.size) as progress:
                await self.cli.download(remote(path), tmp, on_progress=progress)
                downloaded = os.path.join(tmp, node.name)
                if not os.path.isfile(downloaded):
                    # The CLI skips Proton Docs and Sheets.
                    raise CliError(f"{path} can't be downloaded (Proton Docs/Sheets?)", errno.EIO)
                os.replace(downloaded, target)
        actual = os.path.getsize(target)
        if actual != node.size:
            self._update_cached(path, replace(node, size=actual))
        self._evict()
        return target

    def _evict(self, limit=None):
        """Removes the least recently used downloads until they fit in `limit`."""
        limit = self.cache_limit if limit is None else limit
        files = []
        for entry in os.scandir(self._content_dir):
            st = entry.stat()
            files.append((st.st_mtime, st.st_size, entry.path))
        total = sum(size for _, size, _ in files)
        for _, size, file in sorted(files):
            if total <= limit:
                break
            os.unlink(file)
            total -= size
        if total != self.cache_bytes:
            self.cache_bytes = total
            self._write_status()

    def clear_cache(self):
        """Frees the space used by downloaded files; they download again when opened.

        Files still waiting to upload live in staging and are kept. Open files
        keep working, since an unlinked file stays readable through its fd.
        """
        self._evict(limit=0)

    # Writing

    def _is_virtual(self, path):
        return (
            self.hide_from_indexers
            and path == TRACKER_IGNORE
            and TRACKER_IGNORE not in self._listings.get("", (0, {}))[1]
        )

    def _refuse_virtual(self, *paths):
        if any(self._is_virtual(path) for path in paths):
            raise CliError(f"{TRACKER_IGNORE} is provided by the mount", errno.EPERM)

    async def begin_write(self, path, truncate, create=False):
        """Returns a staging file to write `path` into. Pair with end_write()."""
        self._refuse_virtual(path)
        pending = self._pending.get(path)
        if pending and pending.state != "writing":
            await self.settle(path)
            pending = None
        if pending is None:
            source = None
            if create:
                parent, _ = split(path)
                if not (await self.stat(parent)).is_dir:
                    raise CliError(f"not a directory: {parent}", errno.ENOTDIR)
            elif not truncate:
                source = await self.fetch(path)
            staging_dir = tempfile.mkdtemp(dir=self._staging_dir)
            staging = os.path.join(staging_dir, split(path)[1])
            if source:
                shutil.copyfile(source, staging)
            else:
                open(staging, "wb").close()
            # New and truncated files must be uploaded even if nothing is written.
            pending = Pending(staging=staging, dirty=create or truncate)
            self._pending[path] = pending
            self._write_status()
        elif truncate:
            os.truncate(pending.staging, 0)
            pending.dirty = True
        pending.writers += 1
        return pending.staging

    def mark_dirty(self, path):
        self._pending[path].dirty = True

    def end_write(self, path):
        pending = self._pending.get(path)
        if pending is None:
            return
        pending.writers -= 1
        if pending.writers > 0:
            return
        if not pending.dirty:
            self._drop_pending(path)
            return
        pending.state = "queued"
        self._write_status()
        self._send_upload.send_nowait(path)

    def _drop_pending(self, path):
        pending = self._pending.pop(path)
        shutil.rmtree(os.path.dirname(pending.staging), ignore_errors=True)
        pending.done.set()
        self._write_status()

    async def settle(self, path):
        """Waits until `path` has no local-only content."""
        pending = self._pending.get(path)
        if pending and pending.state != "error":
            await pending.done.wait()

    async def _upload_worker(self):
        async for path in self._receive_upload.clone():
            pending = self._pending.get(path)
            if pending is None or pending.state != "queued":
                continue
            pending.state = "uploading"
            self._write_status()
            try:
                size = os.path.getsize(pending.staging)
                with self._transfer(path, "up", size) as progress:
                    await self.cli.upload(pending.staging, remote(split(path)[0]), on_progress=progress)
            except CliError as error:
                pending.state = "error"
                pending.error = str(error)
                self._write_status()
                continue
            # Shown from the staged copy until the refresh brings the real node.
            self._update_cached(path, self._pending_node(split(path)[1], pending, None))
            self._drop_pending(path)
            self.mark_stale(split(path)[0])

    def retry(self, path):
        """Re-queues failed uploads at or below `path`."""
        for pending_path, pending in self._pending.items():
            if pending.state == "error" and is_within(pending_path, path):
                pending.state = "queued"
                pending.error = None
                self._send_upload.send_nowait(pending_path)
        self._write_status()

    async def drain(self):
        for path in list(self._pending):
            await self.settle(path)

    # Tree changes

    async def mkdir(self, path):
        parent, name = split(path)
        if name.startswith(".Trash"):
            # Stops GIO from building a trash folder inside the Drive; Nautilus
            # then offers a permanent delete, which goes to Proton's trash anyway.
            raise CliError("trash folders are not supported", errno.EPERM)
        if name in await self.listdir(parent):
            raise CliError(f"exists: {path}", errno.EEXIST)
        node = await self.cli.create_folder(remote(parent), name)
        self._update_cached(path, node)
        return node

    async def remove(self, path, is_dir):
        self._refuse_virtual(path)
        node = await self.stat(path)
        if node.is_dir != is_dir:
            raise CliError(path, errno.EISDIR if node.is_dir else errno.ENOTDIR)
        if is_dir:
            await self.refresh(path)
        if is_dir and await self.listdir(path):
            raise CliError(f"not empty: {path}", errno.ENOTEMPTY)

        pending = self._pending.get(path)
        if pending:
            if pending.state == "uploading":
                await pending.done.wait()
            else:
                self._drop_pending(path)
                if node.uid is None:
                    return  # Never reached the server.
        await self.cli.trash(remote(path))
        self._update_cached(path, None)
        self.invalidate(path)

    async def rename(self, old, new, overwrite=True):
        self._refuse_virtual(old, new)
        pending = self._pending.get(old)
        if pending and pending.state == "uploading":
            await pending.done.wait()
        source = await self.stat(old)
        old_parent, old_name = split(old)
        new_parent, new_name = split(new)

        existing = (await self.listdir(new_parent)).get(new_name)
        if existing:
            if not overwrite:
                raise CliError(f"exists: {new}", errno.EEXIST)
            if existing.is_dir != source.is_dir:
                raise CliError(new, errno.EISDIR if existing.is_dir else errno.ENOTDIR)
            await self.remove(new, existing.is_dir)

        remote_source = self._listings.get(old_parent, (0, {}))[1].get(old_name)
        if remote_source:
            current = old
            if old_parent != new_parent:
                await self.cli.move(remote(old), remote(new_parent))
                current = posixpath.join(new_parent, old_name)
            if old_name != new_name:
                await self.cli.rename(remote(current), new_name)
            self._update_cached(old, None)
            self._update_cached(new, replace(remote_source, name=new_name))

        for pending_path in [p for p in self._pending if is_within(p, old)]:
            # Local-only content follows the file. The CLI uploads under the
            # staging file's own name, so that must change too.
            pending = self._pending.pop(pending_path)
            moved = new + pending_path[len(old) :]
            staging = os.path.join(os.path.dirname(pending.staging), split(moved)[1])
            os.rename(pending.staging, staging)
            pending.staging = staging
            self._pending[moved] = pending
            if pending.state == "queued":
                self._send_upload.send_nowait(moved)
        self._write_status()

        # Cached listings of a moved folder's subtree move with it.
        for listed in [p for p in self._listings if is_within(p, old)]:
            self._listings[new + listed[len(old) :]] = self._listings.pop(listed)
        self.mark_stale(new_parent)

    # Status for the Nautilus extension and the bar widget

    @contextmanager
    def _transfer(self, path, direction, total):
        transfer_id = self._next_transfer
        self._next_transfer += 1
        transfer = {"path": path, "direction": direction, "done": 0, "total": total}
        self._transfers[transfer_id] = transfer

        def progress(fraction):
            done = int(fraction * total)
            if done != transfer["done"]:
                transfer["done"] = done
                self._write_status()

        self._write_status()
        try:
            yield progress
        finally:
            del self._transfers[transfer_id]
            self._write_status()

    def status(self, path):
        pending = self._pending.get(path)
        if pending is None:
            return "synced"
        if pending.state == "error":
            return f"error: {pending.error}"
        return pending.state

    def _write_status(self):
        if not self.status_file:
            return
        statuses = {path: self.status(path) for path in self._pending}
        tmp = f"{self.status_file}.tmp"
        with open(tmp, "w") as f:
            json.dump(
                {
                    "loggedIn": self.cli.logged_in,
                    "status": statuses,
                    "transfers": list(self._transfers.values()),
                    "cacheBytes": self.cache_bytes,
                    "cacheLimit": self.cache_limit,
                },
                f,
            )
        os.replace(tmp, self.status_file)

    def remove_status(self):
        if self.status_file and os.path.exists(self.status_file):
            os.unlink(self.status_file)
