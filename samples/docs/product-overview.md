---
source_id: "doc-product-overview"
title: "Aurora Insights — Product Overview"
space_key: "DEMO"
breadcrumb: "Handbook > Products > Aurora Insights"
---

## What is Aurora Insights

Aurora Insights is a fictional analytics platform used as demo content for Data Bridge.
It ships three modules: Collector (event ingestion), Warehouse Sync (batch loading into
BigQuery), and Pulse (dashboarding). All demo material in this repository is
self-authored and contains no real company data.

## Module: Collector

Collector receives events over HTTPS and batches them every 30 seconds. Payloads are
validated against a JSON schema; invalid events are routed to a dead-letter bucket in
Cloud Storage for later inspection.

## Module: Warehouse Sync

Warehouse Sync loads batched events into BigQuery hourly. The loader uses partitioned
tables keyed by event date and clusters on customer_id. A backfill mode replays
dead-letter payloads after schema fixes.

## Module: Pulse

Pulse renders dashboards on top of the BigQuery tables. Dashboard queries must stay
under 2 GB scanned per refresh; heavier queries belong in scheduled materializations.

## Pricing model

Aurora Insights is priced per active seat with a usage-based add-on for events above
50 million per month. Enterprise contracts include a fixed annual platform fee.
