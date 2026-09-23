- ClickHouse and Databricks connections no longer follow server-directed
  redirects or result links. A ClickHouse request that gets an HTTP redirect
  now fails instead of following it. Databricks results are fetched inline
  (`use_cloud_fetch=False`) instead of being downloaded from result links.
