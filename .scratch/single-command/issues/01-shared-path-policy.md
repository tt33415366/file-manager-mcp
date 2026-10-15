# 01 — Prefactor: one shared PathPolicy

**What to build:** Make the Allowed Roots swappable at runtime without changing behavior. The path policy gains a way to replace its roots, and every consumer of the roots — file tools, transfer links, File Channel routes, Command Executor (working-directory check), and remote transfer — holds the one policy instance built for the server instead of its own copy or a second policy. Design: `docs/superpowers/specs/2026-10-14-single-command-design.md` (Runtime and live apply).

**Blocked by:** None — can start immediately.

**Status:** done (875ce0a)

- [x] The path policy can replace its roots; after replacement, confinement checks use only the new roots.
- [x] The app builds exactly one path policy, shared by file tools, transfer links, File Channel routes, Command Executor, and remote transfer.
- [x] A test replaces the roots on the shared policy and shows a file tool, a File Channel request, and `run_command`'s `cwd` check all follow the new roots.
- [x] The full existing test suite passes unchanged.
