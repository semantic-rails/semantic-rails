- Attribute downstream embedding-contract instance uses only to receivers with known
  facade types, excluding unrelated objects and test doubles.
- Retain recorded embedding uses while their last identifier appears in any tracked
  consumer Python file; require identifier absence before removing a generated guard.
