---
source_id: "doc-ops-runbook"
title: "Deployment Runbook"
space_key: "DEMO"
breadcrumb: "Handbook > Engineering > Operations"
---

## Release cadence

Production deploys happen every Tuesday and Thursday at 14:00 UTC. Hotfixes may deploy
any time with two approvals. Every deploy must reference a release profile file listing
service image tags.

## Rollback procedure

If error rate exceeds 2% for five consecutive minutes after a deploy, roll back by
re-applying the previous release profile. Rollbacks do not require approvals; announce
in the operations channel afterwards.

## Database migrations

Migrations run in a separate job before the service deploy. Backward-incompatible
migrations require a two-phase rollout: expand first, contract in the following
release. Never combine expand and contract in one release.

## On-call expectations

The on-call engineer acknowledges pages within 10 minutes, and either resolves or
escalates within 30 minutes. Weekly handoff happens Monday 09:00 UTC with a written
summary of open incidents.
