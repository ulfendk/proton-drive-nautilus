#!/usr/bin/env python3
"""Stand-in for the proton-drive CLI, backed by a local directory.

Implements the `filesystem` commands pdfs uses, with the same argument shape
(`<group> <command> --json [options] -- args...`) and NodeEntity-shaped JSON.
$FAKE_DRIVE_ROOT holds `my-files/` and `trash/`; every call is appended to
$FAKE_DRIVE_ROOT/calls.log. Like the real CLI, transfers draw progress
lines only on a terminal; set $FAKE_DRIVE_SLOW to make them take a while.
"""

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

ROOT = os.environ["FAKE_DRIVE_ROOT"]


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def local(remote_path):
    if remote_path != "/my-files" and not remote_path.startswith("/my-files/"):
        fail(f'Path "{remote_path}" is not supported')
    path = os.path.join(ROOT, "my-files", remote_path[len("/my-files/") :])
    return path.rstrip("/")


def existing(remote_path):
    path = local(remote_path)
    if not os.path.exists(path):
        fail(f"Node not found: {os.path.basename(path)}")
    return path


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def node(path):
    st = os.stat(path)
    entity = {
        "uid": f"volume~{st.st_ino}",
        "name": {"ok": True, "value": os.path.basename(path)},
        "type": "folder" if os.path.isdir(path) else "file",
        "creationTime": iso(st.st_ctime),
        "modificationTime": iso(st.st_mtime),
        "isShared": False,
    }
    if os.path.isdir(path):
        entity["folder"] = {"claimedModificationTime": iso(st.st_mtime), "isImported": False}
    else:
        entity["activeRevision"] = {
            "uid": f"revision~{st.st_ino}~{st.st_mtime_ns}~{st.st_size}",
            "claimedSize": st.st_size,
            "storageSize": st.st_size + 100,
            "claimedModificationTime": iso(st.st_mtime),
        }
    return entity


def draw_progress(name):
    if not sys.stdout.isatty():
        return
    steps = 4 if os.environ.get("FAKE_DRIVE_SLOW") else 1
    for step in range(steps + 1):
        print(f"\x1b[?25l\u280b {step * 100 / steps:.2f}% {name} (1 MiB)\n\x1b[?25h", end="", flush=True)
        if steps > 1:
            time.sleep(0.3)


def results(nodes):
    print(json.dumps([{"uid": n["uid"], "ok": True} for n in nodes]))


def main(argv):
    with open(os.path.join(ROOT, "calls.log"), "a") as log:
        log.write(json.dumps(argv) + "\n")

    group, command, *rest = argv
    args = rest[rest.index("--") + 1 :] if "--" in rest else [a for a in rest if not a.startswith("-")]
    if group != "filesystem":
        fail(f"unsupported group {group}")

    if command == "list":
        path = existing(args[0])
        print(json.dumps([node(os.path.join(path, name)) for name in sorted(os.listdir(path))]))
    elif command == "info":
        print(json.dumps(node(existing(args[0]))))
    elif command == "create-folder":
        target = os.path.join(existing(args[0]), args[1])
        if os.path.exists(target):
            fail("A node with the same name already exists")
        os.mkdir(target)
        print(json.dumps(node(target)))
    elif command == "rename":
        source = existing(args[0])
        target = os.path.join(os.path.dirname(source), args[1])
        if os.path.exists(target):
            fail("A node with the same name already exists")
        os.rename(source, target)
        print(json.dumps(node(target)))
    elif command == "move":
        *sources, parent = args
        parent = existing(parent)
        moved = []
        for source in map(existing, sources):
            moved.append(node(source))
            os.rename(source, os.path.join(parent, os.path.basename(source)))
        results(moved)
    elif command == "trash":
        trash = os.path.join(ROOT, "trash")
        os.makedirs(trash, exist_ok=True)
        trashed = []
        for source in map(existing, args):
            trashed.append(node(source))
            shutil.move(source, os.path.join(trash, f"{os.path.basename(source)}.{len(os.listdir(trash))}"))
        results(trashed)
    elif command == "upload":
        *sources, parent = args
        parent = existing(parent)
        for source in sources:
            draw_progress(os.path.basename(source))
            shutil.copyfile(source, os.path.join(parent, os.path.basename(source)))
        print(json.dumps({"successCount": len(sources), "failures": []}))
    elif command == "download":
        *sources, target = args
        for source in map(existing, sources):
            draw_progress(os.path.basename(source))
            shutil.copyfile(source, os.path.join(target, os.path.basename(source)))
        print(json.dumps({"successCount": len(sources), "failures": []}))
    else:
        fail(f"unsupported command {command}")


if __name__ == "__main__":
    main(sys.argv[1:])
