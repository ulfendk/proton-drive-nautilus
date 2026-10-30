import json
import os
import shutil
import threading

from gi import require_version

require_version("Nautilus", "4.1")

from gi.repository import GObject, Gio, Nautilus

MOUNT = os.environ.get("PROTON_DRIVE_MOUNT", os.path.expanduser("~/ProtonDrive"))
STATUS_FILE = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    "proton-drive-nautilus",
    "status.json",
)
RETRY_XATTR = "user.protondrive.retry"

EMBLEMS = {
    "writing": "emblem-synchronizing",
    "queued": "emblem-synchronizing",
    "uploading": "emblem-synchronizing",
    "error": "emblem-important",
    "local": "emblem-unreadable",  # a git repository kept out of the Drive
}


class ProtonDriveExtension(GObject.GObject, Nautilus.InfoProvider, Nautilus.MenuProvider):
    def __init__(self):
        super().__init__()
        # Reads only the daemon's status file, never the mount itself, so a
        # slow or hung daemon can't freeze Nautilus.
        self._statuses = {}
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        self._monitor = Gio.File.new_for_path(os.path.dirname(STATUS_FILE)).monitor_directory(
            Gio.FileMonitorFlags.NONE, None
        )
        self._monitor.connect("changed", self._on_state_changed)
        self._load_statuses()

    def _load_statuses(self):
        try:
            with open(STATUS_FILE) as f:
                statuses = json.load(f)["status"]
        except (OSError, ValueError, KeyError):
            statuses = {}

        changed = set(statuses.items()) ^ set(self._statuses.items())
        self._statuses = statuses
        for path in {path for path, _ in changed}:
            uri = Gio.File.new_for_path(os.path.join(MOUNT, path)).get_uri()
            Nautilus.FileInfo.create_for_uri(uri).invalidate_extension_info()

    def _on_state_changed(self, monitor, file, other_file, event):
        if file.get_basename() == os.path.basename(STATUS_FILE):
            self._load_statuses()

    def _relative_path(self, file):
        location = file.get_location()
        path = location.get_path() if location else None
        if not path or not path.startswith(MOUNT + os.sep):
            return None
        return os.path.relpath(path, MOUNT)

    def update_file_info(self, file):
        path = self._relative_path(file)
        if path is None:
            return Nautilus.OperationResult.COMPLETE
        status = self._statuses.get(path)
        if status:
            file.add_emblem(EMBLEMS[status.split(":", 1)[0]])
        return Nautilus.OperationResult.COMPLETE

    def get_file_items(self, files):
        paths = [self._relative_path(file) for file in files]
        if all(path is None for path in paths):
            return self._upload_items(files)
        failed = [path for path in paths if path and self._statuses.get(path, "").startswith("error")]
        if not failed:
            return []
        item = Nautilus.MenuItem(
            name="ProtonDrive::Retry",
            label="Retry Upload to Proton Drive",
        )
        item.connect("activate", lambda *_: self._retry(failed))
        return [item]

    def _upload_items(self, files):
        local = [file.get_location().get_path() for file in files if file.get_location()]
        if not local or not all(local) or not shutil.which("proton-drive"):
            return []
        item = Nautilus.MenuItem(
            name="ProtonDrive::Upload",
            label="Upload to Proton Drive",
        )
        item.connect("activate", lambda *_: self._upload(local))
        return [item]

    def _retry(self, paths):
        def run():
            for path in paths:
                try:
                    os.setxattr(os.path.join(MOUNT, path), RETRY_XATTR, b"1")
                except OSError:
                    pass

        threading.Thread(target=run, daemon=True).start()

    def _upload(self, paths):
        process = Gio.Subprocess.new(
            [
                "proton-drive",
                "filesystem",
                "upload",
                "--json",
                "--file-conflict-strategy",
                "rename",
                "--folder-conflict-strategy",
                "merge",
                "--",
                *paths,
                "/my-files",
            ],
            Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE,
        )
        names = ", ".join(os.path.basename(path) for path in paths)
        process.communicate_utf8_async(None, None, self._on_uploaded, names)

    def _on_uploaded(self, process, result, names):
        _, _, stderr = process.communicate_utf8_finish(result)
        if process.get_successful():
            self._notify("Uploaded to Proton Drive", names)
        else:
            lines = (stderr or "").strip().splitlines()
            self._notify("Proton Drive upload failed", lines[-1] if lines else names)

    def _notify(self, summary, body):
        if shutil.which("notify-send"):
            Gio.Subprocess.new(["notify-send", "-a", "Proton Drive", summary, body], Gio.SubprocessFlags.NONE)
