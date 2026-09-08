# PrismAgenticPay

Deterministic policy-authority kernel for autonomous agent payments. It verifies an AP2 closed mandate against enterprise budget buckets and PrismThinker egress **before** a settlement rail may move funds.

Specification: [`docs/KERNEL_SPEC.md`](docs/KERNEL_SPEC.md)

```bash
pip install -e ".[dev]"
pytest
```

The HTTP surface lives in `prismagenticpay.api.create_app`.
