# ADR 0037: Source queries are checked against allowlists, not denylists

Status: accepted by the founder, 2026-10-12 (spec 2.3 invariants 1 and 9, 8.1.3, 8.1.4).

## Context

The read-only checks named what to refuse:

- The SQL Server statement check accepted `OPENQUERY` with a `DELETE` inside, `NEXT VALUE FOR`
  and exclusive table hints. SQL Server has no read-only transaction behind the check.
- The Splunk validator refused only NUL bytes, so `outputlookup`, `collect`, `delete` and
  `sendemail` would run on every poll, and the capability check flagged only `output_file`.
- The MySQL grant parser did not expand roles, so write privileges granted through a role
  passed.

Vendors add functions, commands and capabilities. A denylist goes stale without anyone
noticing.

## Decision

1. **SQL, all dialects.** A statement may read only base tables and CTEs. It may call only
   functions on a per-dialect allowlist of scalar and aggregate functions. It must name at
   least one table. Table hints, `NEXT VALUE FOR`, table-valued functions, `OPENQUERY`,
   `OPENROWSET` and `OPENDATASOURCE` are refused.
2. **SQL Server.** `test()` also refuses a login that sees linked servers (`sys.servers`
   besides the local one) or holds INSERT, UPDATE or DELETE at database scope.
3. **MySQL.** `test()` reads the current roles and runs `SHOW GRANTS ... USING` them. A grant
   line the parser cannot read counts as a write finding.
4. **Splunk search.** The search is split into pipeline stages on unquoted `|`. Each stage
   command must be on a built-in list of read-only commands (generating, filtering, eval,
   stats, formatting and extraction commands). Partner needs are added to that list in code,
   when a design partner asks; there is no setting to widen it.
5. **Splunk capabilities.** Any capability that is not on a read list (`search`, `rtsearch`,
   `list_*`, `get_*` and named read capabilities) makes the account WRITE_CAPABLE.

## Alternatives

- Longer denylists: the next vendor release reopens the hole.
- A setting that extends the Splunk allowlist: one typo enables a write command.

## Consequences

- Some legitimate searches and queries are refused until their command or function is added.
  The error names the refused item, so the fix is one line in code.
- SQL Server remains tested against a fake connection until the payer partner install (ADR 0019).
