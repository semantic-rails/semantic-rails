- `test-package` and `project validate --mode tests` run every test: a refused query fails its
  own test, reported with the test id and the refusal's path, message and hint, and the exit
  code comes from the whole run (before, the first refusal stopped the run with one line and no
  test id). `project validate` prints each failing example or test with its id and hint, and
  `project upgrade` lists each example that does not compile with the refusal's path, message
  and hint, on a package that is already up to date too.
