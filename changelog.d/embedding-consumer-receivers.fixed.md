- Attribute downstream embedding-contract instance uses only to receivers with known
  facade types, excluding unrelated objects and test doubles.
- Retain recorded instance-member guards while unresolved receivers still read or write
  the member; require proof of absence before removing them from the generated contract.
