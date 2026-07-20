# Snowflake Showcase Runbook

The repo now includes a live Snowflake showcase package at [configs/semantic_rails/tpch_sf1_showcase](../configs/semantic_rails/tpch_sf1_showcase) backed by `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`.

## Prerequisite

The Snow CLI must be installed, and the connection `semantic_views_trial` must exist locally:

```bash
snow connection list
```

`semantic_views_trial` is the default execution path for this repo. The semantic package references the Snow CLI connection by name and may pass non-secret `snow sql` context overrides: `database`, `schema`, `warehouse`, and `role`.

For Snow CLI browser SSO setups, keep the external-browser authenticator in the local
Snow CLI connection profile. Snow CLI packages should reference the profile by name and
keep account, user, password, authenticator, and token settings out of `package.yml`.

Deployments that do not need Snow CLI can instead author `connection.kind:
snowflake_native` and install `semantic-rails[snowflake]`. Native packages must use env/file
indirection such as `account_env`, `user_env`, `password_env`, `private_key_file`, or `token_file`;
literal secrets in YAML are still rejected. Native browser SSO can be configured directly
with `account_env`, `user_env`, and `authenticator: externalbrowser`.

## Parse And Validate

```bash
uv run semantic-rails parse-config --package tpch_sf1_showcase
uv run semantic-rails validate-config --package tpch_sf1_showcase
```

## Serve The API

```bash
uv run semantic-rails serve --package tpch_sf1_showcase --port 8092
```

The showcase package covers:

- monthly revenue by region
- average order value by market segment
- top nations by revenue
