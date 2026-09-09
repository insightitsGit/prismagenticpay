# Publishing PrismAgenticPay

Publishing this Python library does not deploy a company website and does not
require a Stripe sandbox run. Integrators supply their own provider keys at
runtime. The package itself must not contain secrets.

## Build and validate

Use a clean environment with Python 3.11 or newer:

```sh
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
python -m pytest tests/test_release_hygiene.py -q
python -m pip install 'dist/prismagenticpay-1.4.1-py3-none-any.whl[dev]'
python -m pip check
python -m pytest -q
```

CI runs a wheel-installed suite outside the checkout on Python 3.11–3.14.
The source archive includes public docs and tests; the wheel includes the
console and license. Company handoff notes, operator scripts, `.env` files,
and credentials must not appear in either artifact.

## Secrets

All secrets are read from the process environment. The library ships empty
defaults only. Do not commit `.env`, Stripe keys, PyPI tokens, or signing
seeds. `Settings` omits secret fields from `repr()`.

Upload uses `PYPI_API_TOKEN` from the operator environment. Never put that
token in git, package data, or chat.

```powershell
$env:PYPI_API_TOKEN = "<pypi token>"
./scripts/publish_pypi.ps1
```

GitHub Actions can also publish via Trusted Publisher (`publish.yml`,
environment `pypi`) or a repository environment secret named `PYPI_API_TOKEN`.

## Optional Stripe sandbox evidence

The opt-in test `tests/test_stripe_sandbox.py` is not a publish gate. When an
`sk_test_` key is available:

```powershell
$env:PAP_RUN_STRIPE_SANDBOX = "1"
python -m pytest tests/test_stripe_sandbox.py -v
```

Never substitute a live key.

## After upload

**1.4.1 is live:** https://pypi.org/project/prismagenticpay/1.4.1/

PyPI wheel SHA-256 `1de73d808bfa89ebbc1175d32f1feaae7e0bc08dae574385385ef194ce1e29ee`
and sdist SHA-256 `6d0ad7e43f14e54fb356eca607262fc42a8ff224648e4ae7a52068353327b49f`
match the local build. Install with `python -m pip install prismagenticpay==1.4.1`.
The long description on that release is the README that was packaged at upload
time. A later version is required to refresh PyPI-rendered docs.

References: [Python packaging guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
and [Stripe PaymentIntent API](https://docs.stripe.com/api/payment_intents/create).
