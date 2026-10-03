- Refuse a metric output whose query would filter or bucket it on another
  advertised clock instead of its pinned clock, with guidance to query the bound clock.
- Check temporal overrides through predicates in nested scoped aggregates and aggregate
  filters so an override cannot silently disappear; ambiguous window clocks still refuse.
- Keep metadata for pinned window metrics available by using their resolved clock
  instead of the first advertised compatible clock.
