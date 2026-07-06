---
source_id: "doc-risk-log"
title: "Atlas Migration — Risk Log"
space_key: "DEMO"
breadcrumb: "Projects > Atlas Migration > Risk Log"
---

## R-01 Dual-write drift

Severity: high. During dual-write, the legacy and Atlas pipelines may diverge if a
schema change lands in only one path. Mitigation: schema changes freeze during each
module's dual-write window; reconciliation query runs nightly.

## R-02 September freeze overrun

Severity: high. If Warehouse Sync migration slips past 2026-08-20, Pulse migration
cannot finish before the September freeze. Mitigation: weekly burn-down review; scope
cut option is to defer Pulse read-replica work.

## R-03 Cost regression in BigQuery

Severity: medium. Atlas tables are clustered differently; poorly adapted dashboard
queries could scan more data and raise cost. Mitigation: query cost report per
dashboard before and after cutover; block queries scanning over 2 GB.

## R-04 On-call overload

Severity: low. Running two pipelines doubles alert surface. Mitigation: route legacy
pipeline alerts to a low-urgency channel during dual-write.
