- The semantic layer comparison pack's published Cube SQL excerpts show Cube's generated SQL
  instead of its first character, and Cube's baseline size counts leave out the stretch-only
  members its baseline files hold. MetricFlow's listed weaknesses and README now state that its
  7-day conversion window's boundaries differ from the stated rule. The Cube and KtX runners no
  longer take settings from the caller's environment or a shared `/tmp` directory, and CI fails
  if Cube's start script loses its dev-server guards.
