- The semantic layer comparison pack's published Cube SQL excerpts show Cube's generated SQL
  instead of its first character, and Cube's baseline join count matches its baseline files.
  Each layer's listed weaknesses now state how its q09 and q15 conversion window's boundaries
  differ from the stated rule. The Cube runner no longer passes the caller's environment to
  Cube, the KtX runner caches its wheel in the pack instead of a shared `/tmp` directory and
  imports only the bytes it checked, and CI fails if Cube's start script loses its dev-server
  guards.
