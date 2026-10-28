# proton-drive CLI: what pdfs relies on

Read from the CLI source (github.com/protondriveapps/sdk, `cli/src`). Check it again with `proton-drive help` and `proton-drive filesystem <command> --help` after upgrading.

## Invocation
`proton-drive <group> <command> [options] [--] args...`. Options are parsed with Node's `parseArgs` (strict), so `--` ends the options. `--json`/`-j` gives machine-readable output. A failure exits with code 1 and prints the error message on stderr, e.g. `Node not found: <name>` or `You need to login first`.

## Paths
Remote paths start with a section: `/my-files`, `/devices`, `/shared-with-me`, `/shared-by-me`, `/trash`, `/photos`, `/albums`. pdfs maps the mount root to `/my-files`.

## Commands used (`filesystem` group)
| command | args | `--json` output |
|---|---|---|
| `list` | `path` | array of NodeEntity |
| `info` | `path` | NodeEntity |
| `create-folder` | `parentPath name` | NodeEntity |
| `rename` | `path newName` (same folder only) | NodeEntity |
| `move` | `sourcePath... targetParentPath` | array of `{uid, ok, error}` |
| `trash` | `path...` | array of `{uid, ok, error}` |
| `delete` | `path...` (only items already in `/trash`) | array of `{uid, ok, error}` |
| `upload` | `localPath... parentPath` | `{successCount, ..., failures}` |
| `download` | `path... localFolder` | `{successCount, ..., failures}` |

Upload `--file-conflict-strategy`: `create-new-revision`, `rename`, `replace` (trash remote), `skip`. Folders: `merge`, `rename`, `replace`, `skip`. Download: files `rename`, `remove` (replace local), `skip`. With `--json` the CLI never prompts. Upload names the remote file after the local file's basename. Download skips Proton Docs/Sheets.

## NodeEntity fields pdfs reads
`uid`, `name` (`{ok, value}`), `type` (`file`/`folder`/`album`/`photo`), `mediaType`, `creationTime`, `modificationTime`, `folder.claimedModificationTime`, `activeRevision.{uid, claimedSize, storageSize, claimedModificationTime}`. Dates are ISO 8601 strings.
