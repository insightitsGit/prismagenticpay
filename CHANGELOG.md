# Changelog

## 1.4.1

- Restrict Stripe PaymentIntent confirmation to card payment methods so account
  dashboard redirect-method defaults do not change the rail's contract.
- Add optional `StripeRail(return_url=...)` and `STRIPE_RETURN_URL` service configuration.
- Preserve the configured confirmation fields across idempotent retries.
- Declare httpx as a base dependency and require the tested PrismThinker API (1.2+).
- Add MIT license text, package links, wheel validation, and release automation.

This is an experimental library release. The opt-in Stripe sandbox test is
operator evidence, not a publish gate. The library does not implement a
customer authentication UI for cards requiring additional action. SQLite
persistence is single-host. Secrets stay in the operator environment.
