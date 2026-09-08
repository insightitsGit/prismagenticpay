# PrismAgenticPay system design

The v1.0.0 multi-protocol design is superseded.

**Canonical frozen kernel:** [`KERNEL_SPEC.md`](KERNEL_SPEC.md) (v1.4.0)

Ingress is a single AP2 v0.2 closed payment mandate. Authority is a multi-bucket ledger with hold TTLs. Epistemic egress is strictly `to_chorusgraph()`. Settlement is an explicit `commit` / `release` / `expire` handshake.

Production wiring and recovery: [PRODUCTION.md](PRODUCTION.md). Remediation evidence and remaining release checks: [IMPLEMENTATION_AUDIT.md](IMPLEMENTATION_AUDIT.md).
