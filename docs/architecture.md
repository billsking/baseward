# Directory structure

[English](architecture.md) | [简体中文](architecture.zh-CN.md)

```
app/
  main.py            # app assembly, lifecycle, exception handling
  config.py          # GATEWAY_ environment variables
  netip.py           # source IP normalization and allowlist text parsing (pure functions)
  netallow.py        # runtime source of truth for the source IP allowlist: DB first, env fallback, TTL cache
  services.py        # service container
  db/                # management database ORM, table creation, seeded admin
  security/          # AES-GCM/HKDF/bcrypt, admin sessions, Bearer middleware
  policy/engine.py   # SQL AST policy engine
  policy/scope.py    # token table/column authorization checks and SELECT * rewriting
  masking/           # automatic/manual rules, AST masking plan
  datasources/       # CRUD, connection tests, connection pools; databases.py is the single entry point for accessible databases and per-database policy
  tokens/            # token lifecycle and the four-level scope model
  audit/             # audit write/query/cleanup
  mcp/               # FastMCP tools, execution pipeline and metadata search (introspect.py)
  admin/             # admin console routes, templates, local static assets
migrations/          # 001 init / 002 export channel / 003 credential source IP binding / 004 setting value widened to 1024 / 005 multi-database accessible-databases list and audit database column
scripts/gen_master_key.py
tests/unit/          # no MySQL dependency
tests/integration/   # requires a test MySQL
tests/perf/          # performance benchmark scripts
```
