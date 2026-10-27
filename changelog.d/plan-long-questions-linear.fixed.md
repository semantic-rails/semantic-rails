- `plan` reads a long question in time linear in its length: finding clock phrases ("by
  order date", "weekly") checked every run of words, so a 1,000-word intent took about 20 s
  of CPU. It now takes well under a second, and plans are unchanged.
