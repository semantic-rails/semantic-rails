- The HTTP API (`/api/v1/*`) now applies the same browser checks as `/mcp`: a request whose
  `Origin` is not listed in `SEMANTIC_RAILS_CORS_ORIGINS` is refused with `403`. Both
  surfaces, and the package-load failure response, also refuse a request that reaches the
  server on a loopback address without one well-formed loopback `Host`, with `421`. The new
  `SEMANTIC_RAILS_ALLOWED_HOSTS` lists further host names and applies the `Host` check to
  every request; set it behind a port mapping or a proxy. Clients that send no `Origin`,
  such as the CLI, SDKs and `curl`, are unaffected on loopback.
