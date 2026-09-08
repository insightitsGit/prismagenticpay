# PrismAgenticPay

Deterministic policy-authority kernel for autonomous agent payments. It verifies an AP2 closed mandate against enterprise budget buckets and PrismThinker egress **before** a settlement rail may move funds.

Specification: [`docs/KERNEL_SPEC.md`](docs/KERNEL_SPEC.md)

Production operations: [`docs/PRODUCTION.md`](docs/PRODUCTION.md)

```bash
pip install -e ".[api]"
uvicorn prismagenticpay.runtime:create_production_app --factory --port 8080
```

Console: `http://127.0.0.1:8080/console`

```bash
pip install -e ".[dev]"
pytest
```

The HTTP surface lives in `prismagenticpay.api.create_app`.
