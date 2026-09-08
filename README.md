# PrismAgenticPay

Deterministic policy-authority kernel for autonomous agent payments. It verifies an AP2 closed mandate against enterprise budget buckets and PrismThinker egress **before** a settlement rail may move funds.

Specification: [`docs/KERNEL_SPEC.md`](docs/KERNEL_SPEC.md)

Production operations: [`docs/PRODUCTION.md`](docs/PRODUCTION.md)

Implementation gaps and readiness: [`docs/IMPLEMENTATION_AUDIT.md`](docs/IMPLEMENTATION_AUDIT.md)

Repeatable unit and local HTTP scenario tests: [`docs/LOCAL_TESTING.md`](docs/LOCAL_TESTING.md)

```bash
pip install -r requirements-tested.txt
pip install --no-deps -e .
uvicorn prismagenticpay.runtime:create_production_app --factory --port 8080
```

Configure the registries and stable signing seed described in the production guide before starting. Production supports the single-currency Stripe path; Coinbase/ISO settlement and FX are disabled.

Console: `http://127.0.0.1:8080/console` (requires an audit identity/header in production).

```bash
pip install -e ".[dev]"
pytest
```

The HTTP surface lives in `prismagenticpay.api.create_app`.
