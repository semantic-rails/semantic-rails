- Refuse a metric output whose query would filter or bucket it on another
  advertised clock instead of its pinned clock, with guidance to query the bound clock.
- Check temporal overrides through nested scoped predicates so an override
  cannot silently disappear inside their inputs; ambiguous window clocks still refuse.
