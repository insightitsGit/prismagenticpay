# PrismAgenticPay — Kernel Specification (v1.4.0)

| Field | Value |
|---|---|
| **Version** | 1.4.0 |
| **Status** | Frozen kernel |
| **Core Engine** | PrismThinker contract via `to_chorusgraph()` |
| **Primary Ingress** | AP2 v0.2 closed payment mandate (JWS or SD-JWT) |
| **Settlement** | Signed `AuthorizationDecision` + `commit` / `release` / `expire` |

x402 and TAP are not ingress peers. This product does not clear funds.

## Protocol boundary

```
AP2 closed mandate (JWS / SD-JWT)
        │  verify signature, checkout_hash, cnf, optional open constraints
        ▼
PaymentProposal (integer cents, line items, canonical hash)
        │
        ▼
FactTtlValidator (omit stale and untracked facts)
        │
        ▼
Multi-bucket ledger (session / principal-day / mandate, hold TTL)
        │
        ▼
PrismThinker.evaluate → to_chorusgraph()
        │
        ├─ EXECUTE  → signed AUTHORIZED, hold kept
        ├─ ESCALATE → REVIEW case, hold kept for dual control
        ├─ GATHER   → STALLED case, hold marked gathered (retry allowed)
        └─ REFUSE   → hold released, hash is terminal
        │
        ▼
Settlement rail presents signed decision → commit | release | expire
```

## Invariants

1. External AP2 objects never enter the ledger or evaluator.
2. Stale or unmetadata'd facts are omitted. Missing required facts produce `GATHER`.
3. Settlement requires `AUTHORIZED`, live expiry, exact payment hash, matching mandate hash when a proposal is presented, and a valid Ed25519 signature when a signer is configured.
4. PrismThinker is unmodified. Directives come only from `to_chorusgraph()`.

## AP2 scope (honest)

Implemented:

- ES256 compact JWS
- SD-JWT disclosures (`_sd` digest check) and optional key-binding `sd_hash`
- `checkout_hash` bind to the merchant checkout JWT
- `cnf.jwk` must equal the trusted payment public key when present
- Optional open mandate: `max_amount_cents` and category scope

Not implemented (and not claimed as done):

- Full SD-JWT-VC / kb-sd-jwt ecosystem features beyond the checks above
- AP2 receipt exchange or dispute evidence packages
- Network-operated trust lists

## HTTP API

Production uses `/v1/authorize/ap2` with server-owned authority and authenticated identity scopes. Raw `/v1/authorize` and `/v1/settle` below are development harness routes and return 403 in production. Refunds require a stable logical operation ID. The supported production rail is Stripe; cross-currency settlement is disabled. See [PRODUCTION.md](PRODUCTION.md) for roles and recovery endpoints.


| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/authorize` | Proposal + authority → decision |
| POST | `/v1/settle` | Signed decision + payment hash → commit |
| POST | `/v1/capture` | Rail capture then ledger capture + receipt |
| POST | `/v1/refund` | Rail refund then ledger refund |
| POST | `/v1/reviews/{id}/approve` | Second approver, must not be principal or agent |
| POST | `/v1/reviews/{id}/deny` | Release hold |
| POST | `/v1/gathers/{id}/resume` | Re-authorize after fresh facts |
| GET | `/v1/audit` | Append-only event log |
| GET/POST/DELETE | `/v1/policies` | Policy studio |
| POST | `/v1/policies/simulate` | Dry-run PrismThinker |
| GET | `/console` | Operator console |

## Threat model

| Threat | Control |
|---|---|
| Adapter spoof / unsigned mandate | ES256 verify + optional `cnf.jwk` bind |
| Cart swap after auth | Payment hash includes MCC, category, line items, mandate hash, nonce |
| Replay of settled or abandoned auth | Terminal hash states; gathered is the only retryable release |
| Dual-control bypass | Hold retained on REVIEW; approver ≠ principal/agent |
| Unsigned settlement callback | Ed25519 signature over canonical decision bytes |
| Unknown rail | `allowed_rails` allowlist |
| Stale ERP facts | TTL withhold → GATHER, not degraded trust |
| Process crash | SQLite snapshot ledger (`SqliteAuthorityLedger`) |

## Persistence

`AtomicAuthorityLedger` is the in-memory reference. `SqliteAuthorityLedger` reloads under `BEGIN IMMEDIATE` and atomically commits a full workflow snapshot: holds, decisions, cases, audit, policies and operations. Processing holds reserve capacity across unknown provider outcomes. Local SQLite connections serialize safely; this is not a multi-region cluster.

## Out of kernel scope

FX conversion, refunds, partial capture, PCI card data, ISO 8583 field mapping, and hosted ERP connectors. Those stay on the rail or the enterprise suite.
