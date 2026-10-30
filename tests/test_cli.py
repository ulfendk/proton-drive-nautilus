import errno
import json
import os

from protondrivefs.cli import errno_for, parse_node

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def test_parses_file():
    node = parse_node(fixture("file.json"))
    assert node.name == "report.pdf"
    assert not node.is_dir
    assert node.size == 48213
    assert node.revision == "revision-uid"
    assert node.mtime == 1767225600.0  # claimedModificationTime wins over modificationTime


def test_parses_folder():
    node = parse_node(fixture("folder.json"))
    assert node.name == "Documents"
    assert node.is_dir
    assert node.size == 0


def test_skips_undecryptable_names_and_photos():
    file = fixture("file.json")
    assert parse_node({**file, "name": {"ok": False, "error": {}}}) is None
    assert parse_node({**file, "type": "photo"}) is None


def test_falls_back_to_storage_size():
    file = fixture("file.json")
    revision = {k: v for k, v in file["activeRevision"].items() if k != "claimedSize"}
    assert parse_node({**file, "activeRevision": revision}).size == 48713


def test_maps_cli_messages_to_errno():
    assert errno_for("Node not found: foo") == errno.ENOENT
    assert errno_for("You need to login first") == errno.EACCES
    assert errno_for("A node with the same name already exists") == errno.EEXIST
    assert errno_for("something exploded") == errno.EIO
