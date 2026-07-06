---
source_id: "doc-meeting-atlas-kickoff"
title: "Atlas Migration — Kickoff Meeting Notes (2026-06-15)"
space_key: "DEMO"
breadcrumb: "Projects > Atlas Migration > Meetings"
---

## Attendees and goal

Attendees: Mina (PM), Jae (Backend), Sora (Data), Leo (Infra). Goal: migrate the
legacy event pipeline to the Atlas architecture before the September freeze.

## Decisions

The team agreed to migrate the Collector module first because it has the fewest
downstream consumers. Warehouse Sync migrates second, Pulse last. Dual-write runs for
two weeks per module before cutover.

## Action items

- Jae: draft the Collector dual-write design by 2026-06-22.
- Sora: produce a row-count reconciliation query comparing legacy and Atlas tables by
  2026-06-25.
- Leo: provision the staging Atlas cluster by 2026-06-20.
- Mina: circulate the September freeze calendar to stakeholders by 2026-06-18.

## Open questions

Whether Pulse dashboards need a read-replica during cutover is unresolved; Leo will
benchmark replica lag and report at the next meeting.
