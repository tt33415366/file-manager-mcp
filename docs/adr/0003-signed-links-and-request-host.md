# Signed Links for DUTs, and File Channel links built from the request Host

The Local PC often sits on several networks at once: a Remote Agent reaches it on one interface while a DUT, which can pull or push files over HTTP but must never hold an MCP token, sits on another. So File Channel links stop using one fixed base URL: without `public_base_url` they are built from the validated `Host` of the request that asked for them, and when the Remote Agent names a DUT (`for_host`) the server asks the OS routing table which local address reaches that DUT and returns a Signed Link on it. A Signed Link is the only way to use the File Channel without a bearer token: an HMAC over path, direction, expiry (and for uploads `overwrite` and an optional size cap), keyed by a per-launch random secret so a restart revokes every link (removing the issuing token revokes its links too); each use still re-checks the current Allowed Roots, and the audit log records it as `signed:<issuing token>`. The Host allowlist accepts the Local PC's current interface IPv4 literals checked live, because a DUT interface is often configured after the server starts and an IP literal is not a DNS-rebinding vector.

## Considered Options

- Give each DUT its own bearer token: no new mechanism, but a DUT would get full MCP access.
- A download-only token kind: scoped by direction but not by file, and it changes the token store.
- Persisting the signing key: links survive restarts, but revoking them then needs key rotation and another secret on disk.
- Subnet matching to pick the DUT-side address: needs interface netmasks (not in the Windows stdlib) and misses routed networks.
