- A conversion expression without a supported `matching_mode` now fails with an error that
  names the parameter, lists `first_converted_after_base` and `closest_converted_after_base`
  with what each matches, and returns the sent expression with a mode set. The window error
  lists the supported units. A conversion metric's `inspect` card now shows its own
  expression under `conversion`, so the same conversion can run over another window, such
  as 50 minutes instead of 7 days, without a new metric.
