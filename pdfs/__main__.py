"""Mounts Proton Drive: python -m pdfs ~/ProtonDrive"""

import argparse
import logging
import os
import signal

import pyfuse3
import trio

from .cli import Cli
from .drive import Drive
from .fs import ProtonDriveFS

log = logging.getLogger("pdfs")


def parse_args():
    cache_home = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    parser = argparse.ArgumentParser(prog="pdfs", description=__doc__)
    parser.add_argument("mountpoint", nargs="?", default=os.path.expanduser("~/ProtonDrive"))
    parser.add_argument("--cli", default="proton-drive", help="proton-drive executable")
    parser.add_argument("--cache-dir", default=os.path.join(cache_home, "proton-drive-nautilus"))
    parser.add_argument("--state-dir", default=os.path.join(runtime_dir, "proton-drive-nautilus"))
    parser.add_argument("--list-ttl", type=float, default=30.0, help="seconds a folder listing is reused")
    parser.add_argument("--cache-limit-gb", type=float, default=5.0, help="downloaded content kept locally")
    parser.add_argument(
        "--local-hint",
        action="store_true",
        help="don't report the mount as remote; Nautilus will then thumbnail and count every folder",
    )
    parser.add_argument(
        "--hide-from-indexers",
        action="store_true",
        help="show a virtual .trackerignore in the root so LocalSearch doesn't index the Drive at all",
    )
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


async def serve(drive, stop_timeout=60):
    async with trio.open_nursery() as nursery:
        nursery.start_soon(drive.run)
        nursery.start_soon(pyfuse3.main)
        with trio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
            async for _ in signals:
                log.info("stopping, waiting up to %ss for uploads", stop_timeout)
                with trio.move_on_after(stop_timeout):
                    await drive.drain()
                nursery.cancel_scope.cancel()
                break


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    os.makedirs(args.mountpoint, exist_ok=True)
    os.makedirs(args.state_dir, exist_ok=True)

    drive = Drive(
        Cli(args.cli),
        args.cache_dir,
        list_ttl=args.list_ttl,
        cache_limit=int(args.cache_limit_gb * 1024**3),
        status_file=os.path.join(args.state_dir, "status.json"),
        hide_from_indexers=args.hide_from_indexers,
    )
    options = set(pyfuse3.default_options)
    options.add("fsname=protondrive")
    # GIO only treats a few filesystem types as remote, and fuse.sshfs is the
    # only FUSE one. Remote stops Nautilus from downloading every file for
    # thumbnails and listing every subfolder for item counts.
    options.add("subtype=protondrive" if args.local_hint else "subtype=sshfs")
    if args.debug:
        options.add("debug")

    pyfuse3.init(ProtonDriveFS(drive), args.mountpoint, options)
    log.info("mounted Proton Drive at %s", args.mountpoint)
    try:
        trio.run(serve, drive)
    finally:
        pyfuse3.close(unmount=True)
        drive.save_listings()
        drive.remove_status()


if __name__ == "__main__":
    main()
