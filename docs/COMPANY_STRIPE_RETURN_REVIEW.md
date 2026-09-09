# Company Stripe integration return review

Reviewed 2026-09-08 against the uncommitted company integration on
`pap-stripe-sandbox-integration`, consuming PrismAgenticPay
`e8470b7618d28e63e993e48af76104c5fb14420d`.

## Status

Not ready for deployment review. The company report records 93 passing PAP
tests (one skipped), 13 passing simulated mapping tests, and 33 passing
checkout/billing and dual-path tests. These reported results do not establish
the security of the exposed Flask adapter. The real Stripe sandbox test has
not run because no test key is provisioned. No live key should be used.

## Required company fixes

1. **Authenticate callers and enforce roles at the Flask boundary.**
   `meeting-scheduler/prismagenticpay_bridge/flask_routes.py` exposes authorize,
   approve, capture, and refund without authentication decorators or equivalent
   checks. In particular, `approve_review` forwards any caller's request using
   `service.reviewer_key`. Rejecting an `approver_id` field does not prevent
   impersonation when the adapter supplies that privileged identity itself.
   The inspected app request hooks handle preflight and logging, not caller
   authentication. Require authenticated, authorized company identities and
   preserve purchaser/reviewer separation through to PAP.

2. **Bind mandates and operations to persisted, owned company orders.**
   `authorize_order` constructs the signed order from browser amount, currency,
   SKU, and URL ID without loading the company order or validating ownership.
   Capture and refund forward browser proposals without binding them to the
   URL order or caller. Load trusted order details, verify ownership and state,
   and bind the complete operation to that order before signing or forwarding.

3. **Make routing exclusivity durable and atomic across both payment paths.**
   `dual_path.py` uses a process-local lock around a JSON file. Existing Stripe
   routes only check the file before proceeding; they do not atomically claim
   the order. An existing-path request can pass its check before PAP claims
   the same order, allowing both to proceed. Multiple workers can also race
   on file updates. Use shared transactional order routing reservations for
   both paths, including already-paid and in-flight states. Ensure checkout
   references map to the same persisted order; product identifiers alone are
   not order identifiers.

4. **Preserve trusted configuration on actual service startup.**
   `service.py` calls registry writers which overwrite files, while
   `registries.py` defaults authority to approved with a fresh timestamp.
   Restart must not silently replace revocations or refresh stale authority.
   Separate explicit fixture initialization from normal loading. Public test
   signing seeds and default role keys must remain isolated test fixtures and
   must not become credentials for an exposed staging service.

## Required regression evidence

- Exercise the registered Flask routes: anonymous callers fail; purchasers
  cannot approve; unauthorized users cannot capture or refund another order.
- Reject browser price/currency changes and URL/proposal order mismatches.
- Race both payment paths and separate workers against one order; only one
  provider operation may proceed. Cover existing-path-first, PAP-first,
  already-paid orders, restart, and unrelated orders of the same product.
- Restart through the actual service factory after revoking or expiring
  authority; configuration must remain revoked or expired.
- After offline fixes, provision a sandbox key securely in the company test
  environment and run the guarded real-provider test. Record redacted
  capture/refund/reconciliation results and consumed source revisions.

No demonstrated PAP core defect was identified by this review; these findings
belong to the company adapter. Core production certification remains pending
integration fixes and real-provider evidence. Company integration files and
unrelated company work were not modified by this review.

## Updated return follow-up

The updated company report records 27 bridge tests and 35 targeted company
checks passing. Source inspection confirms new caller authentication, persisted
order binding, SQLite routing, and load-only registry startup. These supersede
the initial implementation findings above, but do not close deployment review.

Two remaining routing defects were returned to the company task:

- Checkout reserves browser-supplied order identifiers before ownership and
  existence validation. `OrderStore.reserve` does not enforce ownership, so a
  caller can reserve another user's order on the Stripe path and block PAP,
  even when the subsequent checkout fails.
- `reserve_existing_path` constructs `default_store` before checking the feature
  flag. Disabled integration still creates/opens the SQLite store and can break
  existing checkout on an unavailable or unwritable database path.

The race test inspected uses threads calling the store, not separate processes
or actual checkout/provider boundaries. Further regression evidence was requested.
An independent rerun did not reach tests: the company virtual environment's base
Python failed to launch, and the source virtual environment lacks Flask. The
reported passing counts remain company-reported, not independently reproduced.
No real Stripe sandbox run occurred. The company task received these findings
and a request to fix and return updated evidence; no PAP core code was changed.
