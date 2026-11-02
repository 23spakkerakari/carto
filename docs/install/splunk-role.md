# Splunk: a search-only role for carto (spec 8.1.3)

carto pulls events with the REST search export endpoint (`/services/search/jobs/export`) using
an authentication token for a dedicated user. The user's role may search only the indexes you
list, and nothing else.

1. **Create a role** `carto_search` (Settings > Roles > New role):
   - Inherit from nothing (do not inherit `user`).
   - Capabilities: `search`, `rest_properties_get` (to read its own context). Grant nothing
     else: carto's test accepts only `search`, `rtsearch`, `list_*`, `get_*`,
     `rest_properties_get`, `rest_apps_view`, `export_results_is_visible` and `pattern_detect`,
     and reports any other capability (for example `schedule_search`, `output_file` or
     `change_own_password`, which the `user` role carries) as write-capable (ADR 0037).
   - Indexes searched by default and allowed: only the indexes carto needs (for example `orders`).
   - Search job limits: concurrent searches 2 (carto's `max_concurrency` default), disk quota as
     your policy requires.
2. **Create a user** `carto` with that role only.
3. **Create an authentication token** (Settings > Tokens) for the user with an expiry that matches
   your rotation policy, and store it in your secret manager; reference it from `sources.yaml`
   as `secret_ref`, never inline.
4. **Test** from carto (Setup > Sources > Test). The test runs a one-minute search with
   `| head 1`, reads the user's roles and capabilities, refuses tokens whose roles carry a
   capability off the read list, and lists the indexes the token can see.

The search in `sources.yaml` may use only Splunk's built-in read-only commands; carto refuses a
search that writes, sends, calls out or starts other searches, and macros, when it loads the
configuration (ADR 0037).
