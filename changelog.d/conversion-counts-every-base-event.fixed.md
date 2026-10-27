- A conversion rate counts every base event in its denominator. A session with no customer
  (anonymous traffic), with a customer the customers table doesn't hold, or with a missing
  lookup for a constant property such as its store now counts as a session that didn't
  convert. Before, such sessions dropped out of the denominator and the rate came back too
  high: one anonymous session among 11 read 1.0 instead of 0.909. A NULL match key still
  never matches another NULL key.
