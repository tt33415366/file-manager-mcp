---
id: 007
title: Design File Channel
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: ["005", "006"]
---

## Question

What is the File Channel's HTTP contract: upload and download endpoints, how a Remote Agent obtains a URL (e.g. a tool returning a short-lived signed URL), how it is authenticated relative to "Choose auth mechanism" (005), and whether range requests / resumable uploads are supported?

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

- **Placement and auth**: routes under `/files` on the same Starlette app and port as `/mcp`, behind the same bearer middleware. No signed URLs: the agent's shell (e.g. `curl`) sends the same `Authorization` header (Cursor Cloud Agents keep it in Runtime Secrets). Paths obey the same confinement rules as the file tools ("Define file tool set" (006)).
- **Download**: `GET /files/download?path=<absolute path>` streams the file (`application/octet-stream`, `Content-Length`, `ETag`, `Last-Modified`, `Content-Disposition` with the file name) with `Range` / `If-Range` support (206/416) via Starlette `FileResponse`. Errors: 400 for a directory or invalid path, 403 outside roots, 404 missing.
- **Resumable chunked upload** (chunks fit Cloudflare Free's 100 MB request cap):
  - `PUT /files/upload?path=<p>&offset=<n>`: writes the body at `offset` into the partial file `<p>.fmcp-partial` in the target directory. `offset=0` starts (or restarts) the partial. Any other offset must equal the partial's current size, else `409` with `{"offset": <current size>}`. Body above 64 MiB returns `413`. Success returns `{"offset": <new size>}`.
  - `GET /files/upload?path=<p>`: returns `{"offset": <partial size or 0>}` to resume after a failure.
  - `POST /files/upload/complete?path=<p>&overwrite=<bool>&sha256=<hex, optional>`: verifies the optional digest (mismatch `422`, partial kept), then renames the partial onto `<p>` with `os.replace` (an existing target without `overwrite=true` returns `409`). Returns `{"path", "size", "sha256"}`.
  - `DELETE /files/upload?path=<p>`: discards the partial.
- **Discovery tools** (so an agent never has to guess URLs):
  - `prepare_download(path)` → `{url, size, modified, curl}`.
  - `prepare_upload(path)` → `{upload_url, complete_url, offset, max_chunk_bytes, curl}`.
  - URLs are built from `public_base_url` when configured, otherwise `http://<host>:<port>`. The `curl` field is a ready-to-run example with a `$FILE_MANAGER_TOKEN` placeholder, never the real token.
