---
id: 015
title: Choose spec document structure and location
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: []
---

## Question

Where does the implementation-ready spec (the map's Destination) live, and how is it structured relative to the tickets that hold each decision?

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

- One file, `docs/spec.md`, written for the implementer: overview, stack, configuration, auth, path rules, MCP tools, File Channel, Jobs, Command Executor, remote transfer, audit logging, concurrency, distribution, connectivity recipes.
- It states contracts directly (it is the implementer's single read) and cites the deciding ticket per section; rationale stays in the tickets and `docs/adr/`.
