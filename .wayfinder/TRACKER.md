# Local markdown tracker

This repo has no remote issue tracker; wayfinder maps live as markdown files here.

## Wayfinding operations

- **Map**: `.wayfinder/<map-slug>/map.md`, frontmatter `labels: [wayfinder:map]`.
- **Ticket (child issue)**: `.wayfinder/<map-slug>/tickets/NNN-<slug>.md`. `NNN` is the ticket id, unique within the map.
- **Ticket frontmatter**:
  ```yaml
  id: 001
  title: <name>
  labels: [wayfinder:research]   # research | prototype | grilling | task
  status: open                   # open | closed
  assignee:                      # empty = unclaimed
  blocked_by: []                 # ticket ids
  ```
- **Claim**: set `assignee` before any work.
- **Blocking**: `blocked_by` lists the ids of blocking tickets. A ticket is unblocked when every listed ticket is `closed`.
- **Frontier**: tickets with `status: open`, all `blocked_by` closed, and empty `assignee`, in id order.
- **Resolution comment**: append a `## Resolution` section to the ticket body, then set `status: closed`.
- **Assets**: link them from the ticket (e.g. a `research/<name>` branch and file path); never paste them in.
