# Implementation audit and remediation ù 2026-09-08

## Current result

The principal code blockers identified in the initial audit have been addressed for the **single-currency Stripe deployment path**. Production certification is still pending real provider sandbox and deployment acceptance. Incomplete Coinbase/ISO settlement and FX were disabled, not represented as finished integrations.

The audit covered design/specification/operations documents, runtime and API wiring, models, AP2 verification, policies/evaluator, authority sources, ledger/SQLite, review/gather cases, audit and receipts, rails, identity checks and deployment configuration. PrismThinker is used without source changes and directives still come from `to_chorusgraph()`.

## Remediation evidence

- **Trusted ingress:** production rejects raw proposals/authority and direct settlement. AP2 endpoint verifies both signatures using purpose-scoped public keys, binds the payment to the authenticated principal/agent, and resolves server-owned authority. Tests reject wrong signatures, issuer, checkout, scope, currency and expired tokens before a hold is created.
- **Fact provenance:** omitted metadata no longer gets fabricated timestamps. Authority snapshots retain source times; stale/future/untracked facts produce GATHER. Configured ERP facts carry connector metadata and missing fields are withheld. Resume refreshes server facts and preserves the original session.
- **Identity and dual control:** high-entropy API keys map to named subjects and explicit roles via SHA-256 digests. A submitted approver name cannot override the authenticated subject. Approval races serialize in the workflow transaction. Policy administration and reconciliation have separate roles.
- **Durability:** decisions, cases, audit history, effective policies, operations and balances share one SQLite transaction. State reloads under the database writer lock. Persistence failure rolls back memory and disk; readiness exercises storage. Receipt signing is stable across restarts.
- **Payment recovery:** capture enters a durable processing state before contacting a rail. That state keeps capacity reserved and blocks ordinary release/expiry/commit. Provider operation IDs and references persist. Unknown outcomes can be retried within 23 hours or reconciled through read-only provider evidence. Competing operations use a durable lease and ledger finalization occurs once.
- **Refund accounting:** every logical refund requires its own operation ID. Equal-amount refunds are distinct when IDs differ; repeated IDs return the original result. Pending refunds reserve refundable balance. Only provider `succeeded` counts as paid/refunded.
- **Signing/binding:** full decision evidence is signed, payment hashes include mandate identity/expiry, authorization cannot outlive the mandate, and receipt amounts reflect partial capture. Empty rail allowlists remain empty. A deployment seed mismatch against an existing database fails startup.
- **Unsafe extensions:** runtime rejects Coinbase/ISO settlement configuration and non-home-currency/caller-supplied FX requests. These modules remain experimental library components outside the supported deployment path.
- **Configuration:** production requires named identity, trust and authority registries plus a stable seed. Docker/CI install the tested dependency versions. Console access is authenticated; rate-limit keys are derived only after authentication.

## Tests

Final local result on 2026-09-08 with Python 3.12.10 and built `dist/` artifacts: **104 passed, 1 skipped**, in 36 seconds. The skipped test is the opt-in Stripe sandbox contract test. Two upstream FastAPI/Starlette deprecation warnings remain. Without `dist/`, the release-artifact hygiene check is also skipped (103 passed, 2 skipped).

`test_local_scenario.py` runs the production factory and a provider simulator over real loopback HTTP. It verifies signed AP2 ingress, raw-route rejection, review role enforcement, self-approval rejection, partial capture, receipt amount/signature, refund constraints, audit history, and repeat-refund/receipt verification after rebuilding the application with the same SQLite database.

`test_durable_workflow.py` injects a lost response after provider payment and a database failure after provider success; restarting and retrying does not duplicate the payment. It covers old-operation read-only reconciliation, definitive cancellation, two SQLite connections racing for budget, concurrent review approvals, atomic rollback, durable gather scope, absent/future metadata, and full decision signature binding.

`test_production_boundaries.py` checks input trust, server fact refresh, mandate expiry, policy roles/persistence, readiness failures, disabled rails, refund pending status and mismatched provider evidence. Existing budget, TTL, policy veto, AP2 and API tests remain active. Existing tests now supply explicit test-source metadata where they intend fresh facts.

`test_stripe_sandbox.py` is opt-in and refuses any key without the `sk_test_` prefix. It performs a real sandbox capture, refund and provider-evidence lookup when enabled. It is operator evidence, not a library publish gate.

## Remaining release work

- Run the opt-in Stripe sandbox test with the intended account and validate any account-specific payment-method/confirmation requirements.
- Validate actual ERP tenant mappings and the authority-grant writer, key provisioning/rotation, TLS gateway, permissions, backups/restores, recovery monitoring and expected throughput.
- Perform deployment-level process-kill/network-fault drills. Local fault injection does not prove infrastructure durability or provider availability.
- Review the breaking canonical/signature/API changes before migrating an existing 1.3 database. Missing historical workflow data cannot be invented.
- Full SD-JWT-VC ecosystem compliance, multi-region operation, Coinbase/ISO settlement and FX are not supported production features.

No real-money transaction, external ERP connection, or Docker deployment was run. See [PRODUCTION.md](PRODUCTION.md) and [LOCAL_TESTING.md](LOCAL_TESTING.md).
