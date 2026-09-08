# Production operations — 1.4.0

This release hardens the single-currency Stripe path. Local tests pass; a live Stripe sandbox run and deployment acceptance are still required before real money. Coinbase/ISO settlement and caller-supplied FX are disabled. ERP mappings must be validated against your tenant.

## Required configuration

- `PAP_ENV=production`
- `PAP_SIGNING_SEED_HEX`: stable, secret 32-byte Ed25519 seed, hex encoded. Back it up securely. The service refuses a different key for an existing workflow database. Receipt signing uses a separate key derived from this seed with domain separation.
- `PAP_DATABASE_URL=sqlite:////data/prismagenticpay.sqlite`: local durable disk, not a shared network filesystem.
- `PAP_IDENTITY_REGISTRY_PATH`: server-owned JSON identity registry (below). Legacy `PAP_API_KEYS` only applies to development.
- `PAP_AUTHORITY_REGISTRY_PATH`: server-owned authority grants with source timestamps and TTLs.
- `PAP_TRUST_REGISTRY_PATH`: public P-256 mandate/checkout verification keys and their allowed purposes.
- `PAP_ALLOWED_RAILS=stripe` and `STRIPE_API_KEY`: initially a sandbox `sk_test_` key.
- `PAP_HOME_CURRENCY=USD` (or your single configured accounting currency).

All callers must use TLS in deployment. Terminate TLS and enforce request-size/connection limits at your gateway. API keys are high-entropy secrets; the server stores only their SHA-256 digests. Provision each human/service with its own key and least-privilege roles. Do not give purchasing agents the review role. Restart the service after changing identity or trust registries. Authority grants are read on each request and should be replaced atomically by a trusted writer.

The database contains payment tokens, proposals, decisions and evidence. Restrict filesystem access and encrypt backups. It contains no supported PAN/CVV fields; Stripe requests require `pm_` payment-method tokens. Do not put production secrets in version control.

### Identity registry

```json
{
  "identities": [
    {
      "subject": "employee-42",
      "key_sha256": "REPLACE_WITH_SHA256_OF_RANDOM_API_KEY",
      "roles": ["authorize", "capture", "refund"],
      "principal_id": "employee-42",
      "agent_id": "purchasing-agent-42"
    },
    {
      "subject": "finance-controller-7",
      "key_sha256": "REPLACE_WITH_DIFFERENT_KEY_DIGEST",
      "roles": ["review", "audit", "reconcile"]
    }
  ]
}
```

Roles are `authorize`, `capture`, `refund`, `review`, `audit`, `policy`, `reconcile`. Review uses the authenticated subject, ignoring the request's optional legacy approver name. Capture/refund must match the authenticated principal and agent. Console HTML requires the `audit` role and an `X-API-Key` header supplied by your authenticated gateway; do not put the key in a URL.

### Trust registry

```json
{
  "issuers": [
    {
      "issuer_id": "payment-issuer",
      "algorithm": "ES256",
      "purposes": ["payment"],
      "jwk": {"kty": "EC", "crv": "P-256", "x": "PUBLIC_X", "y": "PUBLIC_Y"}
    },
    {
      "issuer_id": "checkout-issuer",
      "algorithm": "ES256",
      "purposes": ["checkout"],
      "jwk": {"kty": "EC", "crv": "P-256", "x": "PUBLIC_X", "y": "PUBLIC_Y"}
    }
  ]
}
```

Provision actual trusted public keys; private JWKs are rejected. Payment and checkout signatures are both verified. Optional open mandates use the trusted payment key. Full SD-JWT-VC ecosystem interoperability is not claimed.

### Authority registry

```json
{
  "grants": [
    {
      "principal_id": "employee-42",
      "agent_id": "purchasing-agent-42",
      "mandate_id": "mandate-42",
      "ttl_seconds": 300,
      "authority": {
        "transaction_cap_cents": 10000,
        "session_remaining_cents": 10000,
        "daily_remaining_cents": 20000,
        "mandate_remaining_cents": 15000,
        "vendor_status": "approved",
        "tier": "standard",
        "requires_dual_signature": true,
        "snapshot_taken_at": "REPLACE_WITH_REAL_SOURCE_UTC_TIMESTAMP"
      }
    }
  ]
}
```

A grant is unique per principal/agent/mandate. Its budget fields describe trusted enterprise limits, bounded again by the ledger. A grant optionally names `connector: "sap"`, `"netsuite"`, or `"coupa"`; runtime must have that connector configured. Connector facts replace vendor standing/tier/dual-control fields with their own metadata. Missing/stale/future facts cause GATHER, never fabricated freshness. Refresh grants from an authoritative source; do not simply rewrite timestamps on stale business data.

## Payment lifecycle

1. `POST /v1/authorize/ap2` with `payment_token`, `checkout_jwt`, `payment_issuer_id`, `checkout_issuer_id`, optionally `open_mandate_jwt` and `auth_validity_seconds`. The caller supplies no authority snapshot. The server binds the verified principal/agent to the API identity and resolves authority. Raw `/v1/authorize` and `/v1/settle` are disabled in production.
2. A reviewer calls `/v1/reviews/{case_id}/approve` with `{}` and their own review key. Approval and case resolution are atomic. Expired mandates cannot be approved.
3. `/v1/capture` takes the exact AP2-projected proposal, signed decision, `rail_id: "stripe"`, a `pm_` token and optional partial amount. Capture operation IDs are server generated and stable per reservation.
4. `/v1/refund` additionally requires a caller-generated `operation_id` (8–100 letters, digits, underscores or hyphens). Reuse the same ID for a retry; use a new ID for a distinct refund, even when amounts are equal.
5. `/v1/gathers/{case_id}/resume` accepts `{}` in production and fetches fresh server facts. Its original session scope is preserved.

Decisions, cases, policy edits, audit events, operation references and balances persist together. The signed receipt records the captured amount and original payment hash. Pending capture operations retain budget and cannot be expired/released by ordinary hold operations.

## Recovery

Use a `reconcile` identity to list `GET /v1/operations`; this listing excludes payment tokens and full proposals.

- For a pending operation younger than 23 hours, `POST /v1/operations/{id}/retry` reuses the original provider operation key. A 120-second lease prevents competing workers; wait for it after an abrupt process exit.
- For any age, `POST /v1/operations/{id}/reconcile` with `{}` uses the saved provider reference. If the response was lost before a reference was saved, supply `{"provider_reference": "pi_..."}` or `re_...` found in the provider dashboard. The server performs a GET only and verifies operation ID, payment hash, amount/currency, and refund capture binding.
- Only confirmed provider success updates captured/refunded balances. Confirmed cancellation/failed refund terminates the operation and releases the corresponding reserved capacity. Unknown or mismatched evidence keeps capacity reserved.
- After 23 hours the retry endpoint refuses to create/capture/refund anything. Never clear an unknown operation by deleting its database record or supplying invented evidence.

Stripe can prune idempotency records after at least 24 hours, so our shorter retry window avoids relying on old keys. See [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests).

Deploy an external operational monitor for pending operations, readiness failures, storage space, and backup health. Recovery is an explicit operator endpoint, not an installed background scheduler. Provider declines/actions that do not establish a terminal no-payment outcome remain pending for reconciliation.

## Storage and deployment limits

SQLite uses FULL synchronous durability and `BEGIN IMMEDIATE`, reloads the latest snapshot under the writer lock, and rolls back all workflow changes on failure. Multiple local connections are covered by concurrency tests. This is not a multi-region service, and full snapshots including audit history have a growing write cost; load-test expected volume and retention before rollout. Use SQLite's backup API or a coordinated snapshot that includes WAL state, then test restore.

`/healthz` reports process liveness. `/readyz` checks SQLite integrity and a write transaction. Readiness is not certification that the provider/ERP is reachable. Docker Compose requires `PAP_CONFIG_DIRECTORY` containing `identities.json`, `authority.json`, `trust.json`; the directory is mounted read-only. Provide the seed and Stripe key through your deployment's secret system.

## Upgrade

1.4.0 changes canonical payment hashes/signatures and the HTTP trust boundary. Drain/reconcile old 1.3 payment operations and back up its database before upgrading. Do not expect old signatures to validate under the new canonical format. Existing ledger snapshots can load, but missing legacy cases/operation evidence cannot be reconstructed safely. Retain historical evidence for any manual migration.

See [LOCAL_TESTING.md](LOCAL_TESTING.md) for the sandbox test and local verification commands.
