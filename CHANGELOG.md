# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.1] - 2026-11-14

Initial open-source release.

### Added

- MCP-native database gateway (Streamable HTTP, stateless) with five tools:
  `list_datasources`, `describe_datasource`, `search_objects`, `execute_sql`,
  `issue_export_credential`.
- Four-level token scopes (datasource → database → table → column); `SELECT *` is
  rewritten to the allowed columns.
- AST-based default-deny SQL policy: DDL, `GRANT`, `CALL`, multi-statement, locking
  reads, `INTO OUTFILE` and writes without `WHERE` are blocked.
- Column masking: automatic keyword detection plus manual `mask/hash/null/off` rules.
- Fail-closed audit: intent is recorded before execution and queries are denied when
  the audit store is unavailable.
- MySQL wire-protocol export channel with short-lived, source-IP-bound credentials and
  automatic handoff for oversized MCP results.
- Dual-identity audit (principal + self-asserted actor) for agent platforms, with
  per-token actor policy.
- Short-lived credential exchange and an OpenClaw bridge plugin
  (`integrations/openclaw-baseward`).
- Bilingual (English / Chinese) admin console, MCP texts and documentation.
- SQLite management store by default; external MySQL or PostgreSQL optional.
- Docker Compose deployment with a non-root container image.