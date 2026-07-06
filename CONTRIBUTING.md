# Contributing

## Two-project isolation rule (hard rule)

This repository is an independent, Google-Cloud-native implementation. It shares
*purpose and contracts* with a private sibling project, but:

- **No file may be copied from the sibling repository's data, corpus, evaluation, or
  memory paths.** Porting means re-implementation against documented contracts
  (see `docs/design/architecture.md` §3.2), never vendoring code or data.
- **No internal/employer data may be ingested or committed** — no customer names,
  internal Confluence content, org structures, or credentials. Demo data is limited to
  BigQuery public datasets and self-authored documents (design D-10).
- The `.gitignore` blocks the sibling project's data file patterns (`*questions*.yaml`,
  `*snapshots*`, `*.crowdy*`, `.env`) as a physical guard. Do not weaken these entries.

## Language

All repository content (code, docs, commits) is in **English** — hackathon submission
requirement.
