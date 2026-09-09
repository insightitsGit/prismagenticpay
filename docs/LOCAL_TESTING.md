# Local testing

From the repository root, with Python 3.11 or newer:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-tested.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pytest -q
```

Focused safety regressions:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_regressions.py -v
```

Complete local purchase scenario:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_local_scenario.py -v
```

The scenario starts two HTTP servers on ephemeral `127.0.0.1` ports and stops them when finished. It creates its own temporary SQLite database and test keys; it does not use the repository's existing database or provider credentials. It verifies AP2 signatures, REVIEW and approval, partial capture, receipt amount/signature, refund limits, replay rejection, audit events and persisted balances. The remote payment provider is simulated; no money moves.

If the default temporary directory is inaccessible in a sandbox, use a fresh directory under the workspace:

```powershell
New-Item -ItemType Directory -Force .test-runs | Out-Null
$testRunPath = ".test-runs/run-" + [guid]::NewGuid().ToString("N")
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=$testRunPath
```

Recorded validation used Python 3.12.10 from PATH (`python -m pytest -q`). `.venv\Scripts\python.exe` is equivalent after the venv is created.

Passing tests do not mean all production requirements are complete. Read `IMPLEMENTATION_AUDIT.md` for remaining blockers and the exact boundaries of the scenario.

## Real Stripe sandbox (explicit opt-in)

Set `STRIPE_API_KEY` securely to an `sk_test_` key from the intended sandbox account, then:

```powershell
$env:PAP_RUN_STRIPE_SANDBOX = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_stripe_sandbox.py -v
```

The test sends a $1 sandbox capture using `pm_card_visa`, verifies provider evidence, refunds it, and verifies refund evidence. It refuses live keys. It is skipped by default and was not run during the local hardening work. Remove the opt-in variable afterward if subsequent runs should remain offline.

The default local scenario now uses `create_production_app`, server-owned temporary registries and `/v1/authorize/ap2`; it no longer relies on the raw projection endpoint. The durable workflow suite adds provider-response loss, database failure, recovery, duplicate-refund and concurrent-writer tests.
