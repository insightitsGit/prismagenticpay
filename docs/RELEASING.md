# Publishing PrismAgenticPay

Publishing this Python library does not deploy the company website.

## Build and validate

Use a clean environment with Python 3.11 or newer:

```sh
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
python -m pip install 'dist/prismagenticpay-1.4.1-py3-none-any.whl[dev]'
python -m pip check
python -m pytest -q
```

CI runs a wheel-installed suite outside the checkout on Python 3.11–3.14.
The source archive includes tests; the wheel includes the console and license.
No database, credentials, or company integration code belongs in either artifact.

## Stripe release evidence

Provision `STRIPE_API_KEY` securely in the test process with an `sk_test_`
secret, then set `PAP_RUN_STRIPE_SANDBOX=1` and run
`python -m pytest tests/test_stripe_sandbox.py -v`. Never substitute a live key.
`STRIPE_RETURN_URL` may be set to the integrating application's return endpoint.
The rail is card-only and does not complete customer authentication challenges.
Retain sanitized results for capture, refund, and reconciliation; offline
durable-workflow tests additionally cover recovery and retry behavior.

CLI/MCP sandbox requests validate provider parameters but do not replace this
library execution. Keep pending operations on their original release until
resolved: changing confirmation parameters while replaying an existing Stripe
idempotency key can produce a parameter mismatch. Drain or reconcile pending
operations before upgrading; never rotate their operation IDs to bypass it.

## Publish

Register a PyPI Trusted Publisher for GitHub owner `insightitsGit`, repository
`prismagenticpay`, workflow `publish.yml`, environment `pypi`.
Configure the GitHub `pypi` environment for the intended release permissions.
No publishing token belongs in this repository.

After the sandbox evidence and CI pass, commit the release, tag that commit
`v1.4.1`, and push the tag. Manually dispatch `publish.yml` on that tag. The
workflow verifies the tag/version match, tests built distributions, and uploads
via OIDC. Check the resulting PyPI page and install the published version in a
fresh environment. Record the artifact hashes and source commit in the release.

References: [Python packaging guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
and [Stripe PaymentIntent API](https://docs.stripe.com/api/payment_intents/create).
