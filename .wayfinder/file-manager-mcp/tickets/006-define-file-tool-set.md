---
id: 006
title: Define file tool set
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: []
---

## Question

Which MCP file tools does the server expose (e.g. list, stat, read, write, move, copy, delete, search), what are their parameters and semantics, at what size a transfer must go through the File Channel instead, and how are paths expressed and confined to Allowed Roots (drive letters, UNC paths, symlinks, case)?

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

**Tools** (read-only ones carry `readOnlyHint`, mutating ones `destructiveHint` where data can be lost):

| Tool | Parameters | Result |
|---|---|---|
| `list_roots` | none | Allowed Root paths |
| `list_dir` | `path`, `cursor=None`, `limit=200` (max 1000) | entries `{name, type, size, modified}` sorted by name, `next_cursor` |
| `stat` | `path` | `{path, exists, type, size, modified}` (`exists: false` instead of an error) |
| `read_text` | `path`, `offset=0`, `max_bytes=32768` (max 262144), `encoding="utf-8"` | `{content, offset, bytes_read, total_size, eof}` |
| `write_text` | `path`, `content`, `mode="create"\|"overwrite"\|"append"`, `encoding="utf-8"`, `create_parents=False` | `{path, bytes_written}` |
| `make_dir` | `path` | `{path}` (parents created, existing is fine) |
| `move` | `source`, `destination`, `overwrite=False` | `{source, destination}` |
| `copy` | `source`, `destination`, `overwrite=False` | `{source, destination}` (directories recursive) |
| `delete` | `path`, `recursive=False` | `{path}` (permanent; non-empty dir needs `recursive`) |
| `search` | `path`, `name_glob="*"`, `content_regex=None`, `limit=100` | matches `{path, line?, text?}`, `truncated` |

- `type` is `file`, `dir`, `symlink`, or `other`; `modified` is ISO 8601 UTC.
- `read_text` never splits a UTF-8 character: a slice ending mid-character is trimmed, and `bytes_read` reports what was consumed so `offset + bytes_read` is the next offset. Undecodable content fails with a message pointing to the File Channel.
- `write_text` `overwrite` is atomic (temp file in the same directory + `os.replace`); `create` fails if the file exists.
- `search` walks recursively, skips files over 1 MiB for content matching, and stops at `limit`.
- Tool failures are MCP tool errors (`isError: true`) whose text starts with a stable code: `OUTSIDE_ROOTS`, `NOT_FOUND`, `ALREADY_EXISTS`, `NOT_A_FILE`, `NOT_A_DIRECTORY`, `DIRECTORY_NOT_EMPTY`, `TOO_LARGE`, `NOT_TEXT`, `INVALID_PATH`, `INVALID_ARGUMENT`.

**File Channel threshold**: inline reads are capped at 256 KiB per call and inline writes at 1 MiB; anything larger, or any binary file, goes through the File Channel ("Design File Channel" (007)).

**Paths and confinement**:
- Paths are absolute native paths; both `\` and `/` are accepted (`D:/Projects/a.txt`). Relative paths fail with `INVALID_PATH`.
- Rejected outright: NUL bytes, device namespaces (`\\?\`, `\\.\`), NTFS alternate data streams (any `:` after the drive), and Windows reserved device names (`CON`, `NUL`, `COM1`, ...).
- Confinement: resolve with `os.path.realpath` (follows symlinks and junctions; non-existent tails allowed), then require the result to be equal to or under the realpath of some Allowed Root, compared with `os.path.normcase` (case-insensitive on Windows) and component-wise via `os.path.commonpath`. A symlink pointing outside the roots is therefore denied.
- UNC paths work only when an Allowed Root is itself a UNC path.
- An Allowed Root itself can be listed and written into, but never deleted or moved.
