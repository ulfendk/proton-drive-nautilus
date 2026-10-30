# Proton Drive for Nautilus

Browse your [Proton Drive](https://proton.me/drive) in Nautilus and drag files
in and out, as if it were a local folder. Made for
[Omarchy](https://omarchy.org/), but it works on any Linux desktop with
Nautilus and FUSE.

Proton doesn't have a Linux sync client yet, but it does have an official
[command-line tool](https://proton.me/support/drive-cli). This project mounts
your Drive at `~/ProtonDrive` and turns every file operation into a CLI call:
opening a file downloads it, saving a file uploads it, and deleting moves it to
Proton's trash.

## Features

- **A real folder.** `~/ProtonDrive` is a FUSE mount, so Nautilus, your
  editor, the terminal and every other app can use it.
- **Fast browsing.** A folder you've opened once is answered from a local
  cache and refreshed in the background, including after a restart. Opening a
  folder only fetches its listing; files are downloaded when you open them.
- **Background uploads.** Copying or saving a file writes it locally and
  uploads it after it's closed. Overwriting a file adds a new revision, so
  Proton's version history is kept.
- **Nothing lost by accident.** Deleting moves the item to Proton's trash,
  where you can restore it.
- **Git repositories stay off the Drive.** A folder copied in with a `.git`
  inside is kept on your disk instead of being uploaded. It still shows up and
  works in `~/ProtonDrive`, marked with an emblem.
- **Nautilus integration.** Emblems on files that are still uploading or
  failed, "Retry Upload" on failed files, "Move to Proton Drive Trash" on
  anything in the Drive, and "Upload to Proton Drive" on any local file. `~/ProtonDrive` is bookmarked in the sidebar.
- **Omarchy bar widget.** A cloud icon shows the state at a glance:

  | Icon | Meaning |
  |---|---|
  | dimmed cloud | signed out, not mounted, or the CLI isn't installed |
  | cloud | mounted and up to date |
  | cloud filling up from the bottom | uploading, filled to the percentage done |
  | cloud filling down from the top | downloading, filled to the percentage done |
  | band sweeping through the cloud | a transfer has started but hasn't reported progress yet |
  | pulsing cloud | files are waiting to upload |
  | red cloud | an upload failed |

  The fill uses the theme's accent colour, so it matches whatever theme you
  switch to.

  Click it to sign in, start or stop the mount, open the folder, follow each
  transfer with its own progress bar, and free up the space used by
  downloaded files.
- **Searchable without downloading.** Desktop search (LocalSearch/Tracker)
  indexes your Drive by name, type, size and date, so Nautilus search finds
  files there. The mount refuses indexers' requests to read file contents, so
  indexing never downloads anything.

## Install

### Requirements

- Nautilus with `nautilus-python`, and `fuse3`
- Python packages: `sudo pacman -S python-pyfuse3 python-trio`
- The Proton Drive CLI (see below)

### 1. Install the Proton Drive CLI

On Arch, the AUR package is `proton-drive-cli-git`. Its build can hang on some
networks, so this repo includes a patched PKGBUILD:

```bash
cd packaging/proton-drive-cli-git
makepkg -si
```

The patch makes three changes:

- **Registry:** it downloads dependencies from the public npm registry instead
  of `pkg.protontech.ch`. That server doesn't accept connections from every
  network, and `bun install` waits on it forever. The packages are
  byte-for-byte the same, and bun checks each one against the lockfile's
  checksum.
- **Bun version:** it accepts any Bun from 1.4.2 up, instead of exactly 1.4.2.
- **Worker:** it also builds `proton-drive-worker` from
  [worker/](worker/proton-drive-worker.ts): the CLI's own commands, served by
  one long-running process. The CLI signs in and decrypts your keys every time
  it starts, which makes each folder listing take a few seconds. The worker
  does that once, and then lists a folder in a fraction of a second.

The unpatched AUR package may work for you as is: `yay -S proton-drive-cli-git`.
Without the worker, the mount runs the CLI for every call, so browsing is
slower.
On other distributions, download the CLI from
[proton.me/download/drive/cli](https://proton.me/download/drive/cli/index.html).

### 2. Sign in

```bash
proton-drive auth login
```

This opens your browser. On Omarchy you can also sign in from the bar widget
after step 3.

### 3. Install the mount, extension and widget

```bash
./install.sh
```

The script:

1. Copies the filesystem to `~/.local/share/proton-drive-nautilus`.
2. Installs the Nautilus extension and bookmarks `~/ProtonDrive`.
3. On Omarchy, installs the bar widget and puts it on the right side of the
   bar.
4. Enables and starts the `proton-drive-fs` systemd user service, which mounts
   `~/ProtonDrive` at login.

Run it again to update.

## How it works

| You do | What happens |
|---|---|
| Open a folder | `filesystem list`, the first time; after that it's served from the cache and refreshed in the background |
| Open a file | `filesystem download` the whole file once; later reads come from `~/.cache/proton-drive-nautilus` (5 GB, oldest files removed first) |
| Copy or save a file | written to a local staging file, then `filesystem upload` after close |
| New folder | `filesystem create-folder` |
| Rename or move | `filesystem rename` / `filesystem move` |
| Delete | `filesystem trash` |

Everything except transfers goes to `proton-drive-worker` when it's installed.
Uploads and downloads always run `proton-drive`, because the CLI only reports
their progress on a terminal. If the worker exits, or leaves a request
unanswered for 30 s (`--worker-timeout`), the mount starts it again on the
next call; if it can't start or never answers, the mount runs the CLI
for every call instead. Start the mount with `--worker ''` to do that from the
start.

[docs/cli-reference.md](docs/cli-reference.md) lists the CLI commands and JSON
fields the mount relies on, and the worker's protocol.

### Good to know

- **Delete with "Move to Proton Drive Trash".** It moves a folder to
  Proton's trash in one call, however much is in it. Nautilus's own Delete and
  Move to Trash go file by file, after first listing every folder inside, which
  for a big folder can take hours. Each item deleted that way also goes to
  Proton's trash, so you can still restore it. The menu item isn't offered for
  anything that's still uploading or kept local.
- **Closing a file doesn't mean it's uploaded.** Watch the emblem or the bar
  widget. When the service stops, it waits up to 60 s for uploads to finish.
- **New folders upload once the copy pauses.** A folder and everything in it
  is held back until nothing has been copied into new folders for 3 s
  (`--quiet-seconds`). That way a repository's `.git`, which can be copied
  last, is seen before any of its files are uploaded.
- **Git repositories are kept in `~/.local/share/proton-drive-nautilus/local`**
  (`--local-dir`), at their path in the mount, and survive restarts. Moving
  files into or out of one copies them. If you run `git init` in a folder
  that's already on the Drive, only its `.git` is kept local; its files keep
  syncing. Start the mount with `--upload-git-repos` to upload repositories
  like any other folder.
- **Changes made elsewhere show up on the next look.** A folder older than 5 s
  is refreshed in the background when you open it, so pressing F5 in Nautilus
  shows the result.
- **The mount reports itself as remote** (filesystem type `fuse.sshfs`). That's
  the only FUSE type GIO treats as remote, and it stops Nautilus from
  downloading every image for thumbnails and listing every subfolder to count
  its items. Start the mount with `--local-hint` to turn this off.
- **Downloaded files stay on disk until you free them.** Use "Free up space"
  in the bar widget, or run
  `python3 -c "import os; os.setxattr(os.path.expanduser('~/ProtonDrive'), 'user.protondrive.clear-cache', b'1')"`.
  Files still waiting to upload are never removed.
- **Desktop indexers can't read file contents.** LocalSearch, Tracker and
  KDE's Baloo get names and metadata only, so their full-text search doesn't
  cover Drive files. To keep LocalSearch out of the Drive completely, start
  the mount with `--hide-from-indexers`. That shows an empty `.trackerignore`
  in the mount root, which is never uploaded.
- **Files without an extension are downloaded when listed.** GIO reads the
  first bytes of such files to guess their type, and the CLI can only download
  whole files.
- **Not supported yet:** Proton Docs and Sheets (they're listed, but can't be
  opened), "Shared with me", Photos, and keeping files available offline.

## Troubleshooting

```bash
systemctl --user status proton-drive-fs    # is the mount running?
journalctl --user -u proton-drive-fs -f    # follow its log
cat "$XDG_RUNTIME_DIR/proton-drive-nautilus/status.json"   # uploads and transfers
```

If the mount gets stuck, run `systemctl --user restart proton-drive-fs`. It
unmounts any leftover mount before starting.

## Development

```bash
python -m venv --system-site-packages .venv
.venv/bin/pip install pyfuse3 trio pytest
.venv/bin/python -m pytest
```

The tests mount the real filesystem, backed by
[tests/fake_proton_drive.py](tests/fake_proton_drive.py), a stand-in for the
CLI that keeps files in a local folder, and
[tests/fake_proton_drive_worker.py](tests/fake_proton_drive_worker.py), which
serves it over the worker's protocol. They need `/dev/fuse`, but not a Proton
account.

To run the mount in the foreground against your own Drive:

```bash
systemctl --user stop proton-drive-fs
.venv/bin/python -m protondrivefs ~/ProtonDrive --debug
```

| Path | What's there |
|---|---|
| `protondrivefs/` | the FUSE filesystem: `cli.py` runs the CLI, `drive.py` caches and uploads, `fs.py` is the FUSE layer |
| `nautilus/` | the Nautilus extension |
| `omarchy/regin.proton-drive/` | the Omarchy bar widget |
| `systemd/` | the user service |
| `worker/` | `proton-drive-worker`, built by the PKGBUILD from the CLI's source |
| `packaging/` | the patched CLI PKGBUILD |

## Disclaimer

This is an independent project. It isn't made, endorsed or supported by Proton
AG. Proton and Proton Drive are trademarks of Proton AG.

## Support

If this is useful to you, you can support its development:

- [GitHub Sponsors](https://github.com/sponsors/ulfendk)
- [Buy Me a Coffee](https://buymeacoffee.com/ulfendk)

## License

[MIT](LICENSE)
