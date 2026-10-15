# 05 — Setup guide LAN path and guidance

**What to build:** The Setup guide offers LAN direct with the right address, and the Agent knows how to hand files to a DUT. Design: decision 7.

**Blocked by:** 01, 03

**Status:** done

- [x] `/api/setup` returns `lan_ips`, `lan_default`, and `listen_host`.
- [x] LAN Connection Path with an address dropdown (default preselected); its URL is `http://<ip>:<port>/mcp`; plain-HTTP warning recommends Tailscale.
- [x] LAN is the default path when there is no `public_base_url` and the running listener is a wildcard.
- [x] Running listener on loopback → red guard telling to set Listen host `0.0.0.0` and restart, and a broken wire.
- [x] "This PC" is labelled local testing; the panel's listen-host warning mentions LAN plain HTTP.
- [x] Agent rule, `INSTRUCTIONS`, and the `prepare_*` descriptions mention `for_host` for DUTs.
- [x] Browser check of the LAN path and a screenshot.
