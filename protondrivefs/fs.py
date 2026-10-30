"""pyfuse3 operations: inode bookkeeping on top of Drive."""

import errno
import functools
import logging
import os
import posixpath
import stat
import time

import pyfuse3

from .cli import CliError
from .drive import is_within

log = logging.getLogger(__name__)

STATUS_XATTR = b"user.protondrive.status"
RETRY_XATTR = b"user.protondrive.retry"
CLEAR_CACHE_XATTR = b"user.protondrive.clear-cache"
# Desktop indexers read every file they find to sniff its type and extract
# its text, which here means downloading the whole Drive. Their content reads
# are refused: they still index names and metadata from the listings, and GIO
# falls back to guessing types from file names. Matched against
# /proc/<pid>/comm, which is cut to 15 characters.
INDEXERS = ("localsearch", "tracker-miner", "tracker-extrac", "baloo_file")

# Nautilus reopens a folder on navigation and on F5; treat that as a refresh
# when the listing is older than this, so reloading always shows fresh data.
REOPEN_MAX_AGE = 5.0


def is_indexer(pid):
    # FUSE reports the calling thread, and extractors read on worker threads
    # with names of their own ("pool-0"), so check the process it belongs to.
    try:
        with open(f"/proc/{pid}/status") as f:
            tgid = next(line.split()[1] for line in f if line.startswith("Tgid:"))
        with open(f"/proc/{tgid}/comm") as f:
            return f.read().strip().startswith(INDEXERS)
    except OSError:
        return False


def fuse_errors(method):
    """Turns every failure into a FUSEError; anything else would stop the main loop."""

    @functools.wraps(method)
    async def wrapper(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except pyfuse3.FUSEError:
            raise
        except CliError as error:
            raise pyfuse3.FUSEError(error.errno) from error
        except OSError as error:
            log.exception("%s failed", method.__name__)
            raise pyfuse3.FUSEError(error.errno or errno.EIO) from error
        except Exception as error:
            log.exception("%s failed", method.__name__)
            raise pyfuse3.FUSEError(errno.EIO) from error

    return wrapper


class Handle:
    def __init__(self, path, fd, writable):
        self.path = path
        self.fd = fd
        self.writable = writable


class ProtonDriveFS(pyfuse3.Operations):
    def __init__(self, drive):
        super().__init__()
        self.drive = drive
        self._paths = {pyfuse3.ROOT_INODE: ""}
        self._inodes = {"": pyfuse3.ROOT_INODE}
        self._next_inode = pyfuse3.ROOT_INODE + 1
        self._handles = {}
        self._next_fh = 1
        self._uid = os.getuid()
        self._gid = os.getgid()

    # Bookkeeping

    def _inode(self, path):
        inode = self._inodes.get(path)
        if inode is None:
            inode = self._next_inode
            self._next_inode += 1
            self._inodes[path] = inode
            self._paths[inode] = path
        return inode

    def _path(self, inode):
        try:
            return self._paths[inode]
        except KeyError:
            raise pyfuse3.FUSEError(errno.ENOENT) from None

    def _child(self, parent_inode, name):
        return posixpath.join(self._path(parent_inode), os.fsdecode(name))

    def _remap(self, old, new):
        for path in [p for p in self._inodes if is_within(p, old)]:
            inode = self._inodes.pop(path)
            moved = new + path[len(old) :]
            self._inodes[moved] = inode
            self._paths[inode] = moved
        for handle in self._handles.values():
            if is_within(handle.path, old):
                handle.path = new + handle.path[len(old) :]

    def _attr(self, path, node):
        attr = pyfuse3.EntryAttributes()
        attr.st_ino = self._inode(path)
        if node.is_dir:
            attr.st_mode = stat.S_IFDIR | 0o755
            attr.st_nlink = 2
            attr.st_size = 4096
        else:
            attr.st_mode = stat.S_IFREG | 0o644
            attr.st_nlink = 1
            attr.st_size = node.size
        attr.st_uid = self._uid
        attr.st_gid = self._gid
        mtime_ns = int((node.mtime or time.time()) * 1e9)
        attr.st_mtime_ns = mtime_ns
        attr.st_atime_ns = mtime_ns
        attr.st_ctime_ns = int((node.ctime or node.mtime or time.time()) * 1e9)
        attr.st_blksize = 128 * 1024
        attr.st_blocks = (attr.st_size + 511) // 512
        attr.entry_timeout = 1.0
        attr.attr_timeout = 1.0
        return attr

    async def _stat_attr(self, path):
        return self._attr(path, await self.drive.stat(path))

    def _new_handle(self, path, fd, writable):
        fh = self._next_fh
        self._next_fh += 1
        self._handles[fh] = Handle(path, fd, writable)
        return fh

    # Metadata

    @fuse_errors
    async def lookup(self, parent_inode, name, ctx=None):
        if name == b".":
            return await self._stat_attr(self._path(parent_inode))
        if name == b"..":
            return await self._stat_attr(posixpath.dirname(self._path(parent_inode)))
        return await self._stat_attr(self._child(parent_inode, name))

    @fuse_errors
    async def getattr(self, inode, ctx=None):
        return await self._stat_attr(self._path(inode))

    @fuse_errors
    async def setattr(self, inode, attr, fields, fh, ctx):
        path = self._path(inode)
        if fields.update_size:
            handle = self._handles.get(fh) if fh is not None else None
            if handle and handle.writable:
                os.ftruncate(handle.fd, attr.st_size)
                self.drive.mark_dirty(path)
            else:
                staging = await self.drive.begin_write(path, truncate=attr.st_size == 0)
                os.truncate(staging, attr.st_size)
                self.drive.mark_dirty(path)
                self.drive.end_write(path)
        # Modes, owners and timestamps aren't stored by Proton Drive; accept and ignore.
        return await self._stat_attr(path)

    @fuse_errors
    async def statfs(self, ctx):
        stats = pyfuse3.StatvfsData()
        stats.f_bsize = stats.f_frsize = 4096
        stats.f_blocks = stats.f_bfree = stats.f_bavail = 1 << 30
        stats.f_files = stats.f_ffree = stats.f_favail = 1 << 20
        stats.f_namemax = 255
        return stats

    @fuse_errors
    async def getxattr(self, inode, name, ctx):
        if name == STATUS_XATTR:
            return self.drive.status(self._path(inode)).encode()
        raise pyfuse3.FUSEError(pyfuse3.ENOATTR)

    @fuse_errors
    async def setxattr(self, inode, name, value, ctx):
        if name == CLEAR_CACHE_XATTR:
            self.drive.clear_cache()
            return
        if name == RETRY_XATTR:
            path = self._path(inode)
            self.drive.mark_stale(path)
            self.drive.retry(path)
            return
        raise pyfuse3.FUSEError(errno.ENOTSUP)

    @fuse_errors
    async def listxattr(self, inode, ctx):
        return []

    # Directories

    @fuse_errors
    async def opendir(self, inode, ctx):
        path = self._path(inode)
        await self.drive.listdir(path, max_age=REOPEN_MAX_AGE)
        return inode

    @fuse_errors
    async def readdir(self, fh, start_id, token):
        path = self._path(fh)
        entries = sorted((await self.drive.listdir(path)).items())
        for index in range(start_id, len(entries)):
            name, node = entries[index]
            child = posixpath.join(path, name)
            if not pyfuse3.readdir_reply(token, os.fsencode(name), self._attr(child, node), index + 1):
                break

    @fuse_errors
    async def releasedir(self, fh):
        pass

    @fuse_errors
    async def mkdir(self, parent_inode, name, mode, ctx):
        path = self._child(parent_inode, name)
        return self._attr(path, await self.drive.mkdir(path))

    @fuse_errors
    async def rmdir(self, parent_inode, name, ctx):
        await self.drive.remove(self._child(parent_inode, name), is_dir=True)

    @fuse_errors
    async def unlink(self, parent_inode, name, ctx):
        await self.drive.remove(self._child(parent_inode, name), is_dir=False)

    @fuse_errors
    async def rename(self, old_parent, old_name, new_parent, new_name, flags, ctx):
        if flags & pyfuse3.RENAME_EXCHANGE:
            raise pyfuse3.FUSEError(errno.EINVAL)
        old = self._child(old_parent, old_name)
        new = self._child(new_parent, new_name)
        await self.drive.rename(old, new, overwrite=not flags & pyfuse3.RENAME_NOREPLACE)
        self._remap(old, new)

    # Files

    @fuse_errors
    async def open(self, inode, flags, ctx):
        path = self._path(inode)
        if flags & os.O_ACCMODE == os.O_RDONLY:
            if is_indexer(ctx.pid):
                log.debug("refusing indexer pid %s: %s", ctx.pid, path)
                raise pyfuse3.FUSEError(errno.EACCES)
            fd = os.open(await self.drive.fetch(path), os.O_RDONLY)
            # The claimed size can be off; direct I/O stops the kernel from
            # clipping reads to a stale st_size.
            info = pyfuse3.FileInfo(fh=self._new_handle(path, fd, False))
            info.direct_io = os.fstat(fd).st_size != (await self.drive.stat(path)).size
            return info
        staging = await self.drive.begin_write(path, truncate=bool(flags & os.O_TRUNC))
        fd = os.open(staging, os.O_RDWR)
        return pyfuse3.FileInfo(fh=self._new_handle(path, fd, True))

    @fuse_errors
    async def create(self, parent_inode, name, mode, flags, ctx):
        path = self._child(parent_inode, name)
        staging = await self.drive.begin_write(path, truncate=True, create=True)
        fd = os.open(staging, os.O_RDWR)
        info = pyfuse3.FileInfo(fh=self._new_handle(path, fd, True))
        return info, await self._stat_attr(path)

    @fuse_errors
    async def read(self, fh, offset, length):
        return os.pread(self._handles[fh].fd, length, offset)

    @fuse_errors
    async def write(self, fh, offset, buf):
        handle = self._handles[fh]
        self.drive.mark_dirty(handle.path)
        return os.pwrite(handle.fd, buf, offset)

    @fuse_errors
    async def flush(self, fh):
        pass

    @fuse_errors
    async def fsync(self, fh, datasync):
        pass

    @fuse_errors
    async def release(self, fh):
        handle = self._handles.pop(fh)
        os.close(handle.fd)
        if handle.writable:
            self.drive.end_write(handle.path)
