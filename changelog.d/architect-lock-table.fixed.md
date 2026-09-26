- A long-running process that authors many package directories no longer keeps one Architect
  lock per directory in memory: a project's in-process lock is dropped once no thread holds or
  waits for it.
