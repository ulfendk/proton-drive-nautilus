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

# A folder that gets a .git while it isn't on the server yet is a git
# repository being copied in. It's kept in the local store instead, and served
# from there; a .git added to a folder that's already on the server stays
# local by itself.
GIT_DIR = ".git"


def remote(path):
    return f"{REMOTE_ROOT}/{path}" if path else REMOTE_ROOT


def split(path):
    parent, name = posixpath.split(path)
    return parent, name


def is_within(path, ancestor):
    return path == ancestor or ancestor == "" or path.startswith(ancestor + "/")


@dataclass
class Pending:
    """A file whose newest content only exists locally, in `staging`, or a
    folder (`is_dir`, no staging) that isn't on the server yet."""

    staging: str | None
    state: str = "writing"  # writing -> queued -> uploading -> (gone) | error
    writers: int = 0
    dirty: bool = False
    error: str | None = None
    is_dir: bool = False
    created: float = field(default_factory=time.time)
    done: trio.Event = field(default_factory=trio.Event)


class Drive:
    def __init__(
        self,
        cli,
        cache_dir,
        list_ttl=30.0,
        cache_limit=5 * 1024**3,
        status_file=None,
        hide_from_indexers=False,
        local_dir=None,
        keep_git_repos_local=True,
        quiet_seconds=3.0,
    ):
        self.cli = cli
        self.hide_from_indexers = hide_from_indexers
        self.list_ttl = list_ttl
        self.cache_limit = cache_limit
        self.status_file = status_file
        self.keep_git_repos_local = keep_git_repos_local
        self.quiet_seconds = quiet_seconds
        # Git repositories live here, at their path in the mount, and never
        # upload. Unlike staging it's kept across restarts.
        self._local_dir = local_dir or os.path.join(cache_dir, "local")
        self._roots_file = os.path.join(self._local_dir, "roots.json")
        self._load_roots()
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
        self._pending = {}  # path -> Pending
        self._last_new_change = float("-inf")  # trio time of the last change inside a new folder
        self._send_upload, self._receive_upload = trio.open_memory_channel(1000)
        self._upload_slots = None
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
        local = self._local_path(path)
        if local is not None:
            entries = {entry.name: self._local_node(entry.path) for entry in os.scandir(local)}
        elif self._is_new_dir(path):
            entries = {}
        else:
            cached = self._listings.get(path)
            if cached is None:
                await self.refresh(path)
                cached = self._listings[path]
            elif time.time() - cached[0] > (self.list_ttl if max_age is None else max_age):
                self._refresh_soon(path)
            entries = dict(cached[1])
            if path == "" and self.hide_from_indexers:
                entries.setdefault(TRACKER_IGNORE, TRACKER_IGNORE_NODE)
            for root in self._local_roots:
                parent, name = split(root)
                if parent == path and os.path.isdir(self._store(root)):
                    entries[name] = self._local_node(self._store(root))
        for pending_path, pending in self._pending.items():
            parent, name = split(pending_path)
            if parent == path:
                entries[name] = self._pending_node(name, pending, entries.get(name))
        return entries

    def _pending_node(self, name, pending, existing):
        if pending.is_dir:
            return Node(uid=None, name=name, is_dir=True, size=0, mtime=pending.created, ctime=pending.created)
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
        self._upload_slots = trio.Semaphore(upload_workers)
        async with trio.open_nursery() as nursery:
            self._nursery = nursery
            nursery.start_soon(self._dispatch_uploads)
            while True:
                await trio.sleep(10)
                self.save_listings()

    # Reading

    async def fetch(self, path):
        """Returns a local file holding the current content of `path`."""
        pending = self._pending.get(path)
        if pending and pending.is_dir:
            raise CliError(f"is a directory: {path}", errno.EISDIR)
        if pending:
            return pending.staging
        local = self._local_path(path)
        if local is not None:
            if os.path.isdir(local):
                raise CliError(f"is a directory: {path}", errno.EISDIR)
            return local
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
        if create:
            self._keep_repository_local(path)
            self._note_change(path)
        pending = self._pending.get(path)
        if pending and pending.is_dir:
            raise CliError(f"is a directory: {path}", errno.EISDIR)
        local = self._local_path(path)
        if local is not None and pending is None:
            if create or truncate:
                open(local, "wb").close()
            elif not os.path.isfile(local):
                raise CliError(f"not found: {path}", errno.ENOENT)
            return local
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
        pending = self._pending.get(path)
        if pending:  # Files kept local are written in place.
            pending.dirty = True
            self._note_change(path)

    def end_write(self, path):
        pending = self._pending.get(path)
        if pending is None:
            return
        pending.writers -= 1
        if pending.writers > 0:
            return
        local = self._local_path(path)
        if local is not None:
            # Was open when its repository was moved to the local store.
            shutil.move(pending.staging, local)
            self._drop_pending(path)
            return
        if not pending.dirty:
            self._drop_pending(path)
            return
        pending.state = "queued"
        self._write_status()
        self._send_upload.send_nowait(path)

    def _drop_pending(self, path):
        pending = self._pending.pop(path)
        if pending.staging:
            shutil.rmtree(os.path.dirname(pending.staging), ignore_errors=True)
        pending.done.set()
        self._write_status()

    async def settle(self, path):
        """Waits until `path` has no local-only content."""
        pending = self._pending.get(path)
        if pending and pending.state != "error":
            await pending.done.wait()

    async def _dispatch_uploads(self):
        async for path in self._receive_upload:
            self._nursery.start_soon(self._upload, path)

    async def _upload(self, path):
        pending = self._pending.get(path)
        if pending is None or pending.state != "queued":
            return
        parent, name = split(path)
        if pending.is_dir or self._in_new_folder(path):
            # A copy creates a folder before what's in it, and a repository's
            # .git can come last. Nothing in a new folder is sent until the
            # copy has paused, so the whole repository can still be kept local.
            await self._wait_quiet()
            while (folder := self._pending.get(parent)) is not None and folder.state != "error":
                await folder.done.wait()
            if self._pending.get(path) is not pending:
                return  # Moved, deleted or kept local meanwhile.
            if folder is not None:
                self._fail(pending, f"folder {parent} couldn't be created")
                return
        async with self._upload_slots:
            if self._pending.get(path) is not pending or pending.state != "queued":
                return
            pending.state = "uploading"
            self._write_status()
            try:
                if pending.is_dir:
                    node = await self.cli.create_folder(remote(parent), name)
                else:
                    size = os.path.getsize(pending.staging)
                    with self._transfer(path, "up", size) as progress:
                        await self.cli.upload(pending.staging, remote(parent), on_progress=progress)
                    # Shown from the staged copy until the refresh brings the real node.
                    node = self._pending_node(name, pending, None)
            except CliError as error:
                self._fail(pending, str(error))
                return
        self._update_cached(path, node)
        if pending.is_dir:
            self._listings.setdefault(path, (time.time(), {}))
        self._drop_pending(path)
        self.mark_stale(parent)

    def _fail(self, pending, message):
        pending.state = "error"
        pending.error = message
        pending.done.set()
        self._write_status()

    def retry(self, path):
        """Re-queues failed uploads at or below `path`."""
        for pending_path, pending in self._pending.items():
            if pending.state == "error" and is_within(pending_path, path):
                pending.state = "queued"
                pending.error = None
                pending.done = trio.Event()
                self._send_upload.send_nowait(pending_path)
        self._write_status()

    async def drain(self):
        for path in list(self._pending):
            await self.settle(path)

    # New folders and git repositories

    def _is_new_dir(self, path):
        pending = self._pending.get(path)
        return pending is not None and pending.is_dir

    def _in_new_folder(self, path):
        while path:
            path = split(path)[0]
            if self._is_new_dir(path):
                return True
        return False

    def _note_change(self, path):
        if self._is_new_dir(path) or self._in_new_folder(path):
            self._last_new_change = trio.current_time()

    async def _wait_quiet(self):
        while (left := self._last_new_change + self.quiet_seconds - trio.current_time()) > 0:
            await trio.sleep(left)

    def _store(self, path):
        return os.path.join(self._local_dir, "files", path)

    def _local_path(self, path):
        """Where `path` lives if it's in a repository kept local, else None."""
        if any(is_within(path, root) for root in self._local_roots):
            return self._store(path)
        return None

    def _local_node(self, local):
        st = os.stat(local)
        return Node(
            uid=None,
            name=os.path.basename(local),
            is_dir=os.path.isdir(local),
            size=st.st_size,
            mtime=st.st_mtime,
            ctime=st.st_ctime,
        )

    def _keep_repository_local(self, path):
        """Called before `path` is created: when it's a .git, keeps its repository local."""
        parent, name = split(path)
        if name != GIT_DIR or not self.keep_git_repos_local or self._local_path(path) is not None:
            return
        folder = self._pending.get(parent)
        if folder is None or not folder.is_dir or folder.state not in ("queued", "error"):
            # The folder is on the server, with its files; only .git stays here.
            root = path
            os.makedirs(os.path.dirname(self._store(root)), exist_ok=True)
        else:
            root = parent
            log.info("keeping git repository %s out of the Drive", root)
            for pending_path in sorted(p for p in self._pending if is_within(p, root)):
                pending = self._pending[pending_path]
                local = self._store(pending_path)
                if pending.is_dir:
                    os.makedirs(local, exist_ok=True)
                    self._drop_pending(pending_path)
                elif pending.writers == 0:
                    # Open files follow in end_write().
                    os.makedirs(os.path.dirname(local), exist_ok=True)
                    shutil.move(pending.staging, local)
                    self._drop_pending(pending_path)
        self._local_roots = {r for r in self._local_roots if not is_within(r, root)} | {root}
        self._save_roots()
        self._write_status()

    def _load_roots(self):
        try:
            with open(self._roots_file) as f:
                self._local_roots = {root for root in json.load(f) if os.path.lexists(self._store(root))}
        except (OSError, ValueError, TypeError):
            self._local_roots = set()

    def _save_roots(self):
        os.makedirs(self._local_dir, exist_ok=True)
        tmp = f"{self._roots_file}.tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(self._local_roots), f)
        os.replace(tmp, self._roots_file)

    def _prune_store(self, local_dir):
        """Removes folders left empty in the store above a removed or moved repository."""
        top = self._store("").rstrip("/")
        while local_dir.startswith(top + "/"):
            try:
                os.rmdir(local_dir)
            except OSError:
                return
            local_dir = os.path.dirname(local_dir)

    # Tree changes

    async def mkdir(self, path):
        parent, name = split(path)
        if name.startswith(".Trash"):
            # Stops GIO from building a trash folder inside the Drive; Nautilus
            # then offers a permanent delete, which goes to Proton's trash anyway.
            raise CliError("trash folders are not supported", errno.EPERM)
        if name in await self.listdir(parent):
            raise CliError(f"exists: {path}", errno.EEXIST)
        self._keep_repository_local(path)
        local = self._local_path(path)
        if local is not None:
            os.mkdir(local)
            return self._local_node(local)
        # Created on the server by an upload task, once whatever is copying
        # into it has paused.
        pending = Pending(staging=None, state="queued", dirty=True, is_dir=True)
        self._pending[path] = pending
        self._note_change(path)
        self._write_status()
        self._send_upload.send_nowait(path)
        return self._pending_node(name, pending, None)

    async def remove(self, path, is_dir):
        self._refuse_virtual(path)
        node = await self.stat(path)
        if node.is_dir != is_dir:
            raise CliError(path, errno.EISDIR if node.is_dir else errno.ENOTDIR)
        local = self._local_path(path)
        if is_dir and local is None and not self._is_new_dir(path):
            await self.refresh(path)
        if is_dir and await self.listdir(path):
            raise CliError(f"not empty: {path}", errno.ENOTEMPTY)

        pending = self._pending.get(path)
        if local is not None:
            if pending:
                self._drop_pending(path)
            if os.path.lexists(local):
                os.rmdir(local) if is_dir else os.unlink(local)
            if path in self._local_roots:
                self._local_roots.discard(path)
                self._save_roots()
                self._prune_store(os.path.dirname(local))
            self._write_status()
            return
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
        old_local, new_local = self._local_path(old), self._local_path(new)
        moves_root = old in self._local_roots
        if (old_local is None) != (new_local is None) and not (moves_root and new_local is None):
            # In or out of a repository kept local; mv and Nautilus copy instead.
            raise CliError(f"{old} -> {new} crosses the local store", errno.EXDEV)
        pending = self._pending.get(old)
        if pending and pending.state == "uploading":
            await pending.done.wait()
        source = await self.stat(old)
        old_parent, old_name = split(old)
        new_parent, new_name = split(new)
        self._note_change(old)
        self._note_change(new)

        existing = (await self.listdir(new_parent)).get(new_name)
        if existing:
            if not overwrite:
                raise CliError(f"exists: {new}", errno.EEXIST)
            if existing.is_dir != source.is_dir:
                raise CliError(new, errno.EISDIR if existing.is_dir else errno.ENOTDIR)
            await self.remove(new, existing.is_dir)

        if old_local is not None:
            target = self._store(new)
            if os.path.lexists(old_local):
                if moves_root:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                os.rename(old_local, target)
            if moves_root:
                self._local_roots.discard(old)
                if new_local is None:
                    self._local_roots.add(new)
                self._save_roots()
                self._prune_store(os.path.dirname(old_local))
            self._move_pending(old, new)
            return

        remote_source = self._listings.get(old_parent, (0, {}))[1].get(old_name)
        if remote_source:
            if self._is_new_dir(new_parent):
                # The CLI can't move into a folder that isn't on the server yet.
                raise CliError(f"{new_parent} isn't on the server yet", errno.EXDEV)
            current = old
            if old_parent != new_parent:
                await self.cli.move(remote(old), remote(new_parent))
                current = posixpath.join(new_parent, old_name)
            if old_name != new_name:
                await self.cli.rename(remote(current), new_name)
            self._update_cached(old, None)
            self._update_cached(new, replace(remote_source, name=new_name))

        self._move_pending(old, new)

        # Cached listings and repositories kept local in a moved folder's
        # subtree move with it.
        for listed in [p for p in self._listings if is_within(p, old)]:
            self._listings[new + listed[len(old) :]] = self._listings.pop(listed)
        roots = [root for root in self._local_roots if is_within(root, old)]
        for root in roots:
            moved = new + root[len(old) :]
            os.renames(self._store(root), self._store(moved))
            self._local_roots.discard(root)
            self._local_roots.add(moved)
        if roots:
            self._save_roots()
        self.mark_stale(new_parent)

    def _move_pending(self, old, new):
        for pending_path in [p for p in self._pending if is_within(p, old)]:
            # Local-only content follows the file. The CLI uploads under the
            # staging file's own name, so that must change too.
            pending = self._pending.pop(pending_path)
            moved = new + pending_path[len(old) :]
            if pending.staging:
                staging = os.path.join(os.path.dirname(pending.staging), split(moved)[1])
                os.rename(pending.staging, staging)
                pending.staging = staging
            self._pending[moved] = pending
            if pending.state == "queued":
                self._send_upload.send_nowait(moved)
        self._write_status()

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
        if self._local_path(path) is not None:
            return "local"
        pending = self._pending.get(path)
        if pending is None:
            return "synced"
        if pending.state == "error":
            return f"error: {pending.error}"
        return pending.state

    def _write_status(self):
        if not self.status_file:
            return
        # Only a repository's top folder is listed, not everything in it.
        statuses = {path: self.status(path) for path in [*self._pending, *self._local_roots]}
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
