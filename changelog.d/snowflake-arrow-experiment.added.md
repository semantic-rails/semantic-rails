- Add an opt-in `snowflake_adbc` connector experiment with bound row-filter
  parameters, Arrow decimal results, query tags, and session statement timeouts.
- Keep Snowflake ADBC native driver selection under runtime operator control;
  package connection options cannot select a driver library or manifest.
- Refuse Snowflake ADBC timestamp overflow and temporal values with nonzero
  sub-microsecond precision instead of returning incorrect dates or losing precision.
- Validate Snowflake ADBC authentication sources when loading packages and during
  guided setup; refuse named profiles that this connector does not use.
