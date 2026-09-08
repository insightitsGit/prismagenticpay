# Company agent handoff: consume PrismAgenticPay and validate Stripe

## Assignment and return address

The user authorized committing/pushing PrismAgenticPay and handing it to the company agent to consume locally, test against the company's Stripe integration, and hand results back.

- Receiving project: `C:\code\InsightitsAIAgent` (company website).
- Source project: `C:\code\PrismAgenticPay`.
- Source Git: https://github.com/insightitsGit/prismagenticpay.git
- Branch: `main`; implementation baseline: `e8470b7618d28e63e993e48af76104c5fb14420d` (version 1.4.0).
- Return task ID: `01a082bd-c186-7640-9244-b132f3a72a03`, host `local`, title **Audit implementation and add tests**.
- Receiving task ID: `01a06093-dc8c-7eb3-a7f9-ab10ebd3141c`, host `local`.

Read `docs/PRODUCTION.md`, `docs/LOCAL_TESTING.md`, and `docs/IMPLEMENTATION_AUDIT.md` in the source repository before integrating. Baseline: 93 tests passed, one optional real Stripe sandbox test skipped. No real provider sandbox or company checkout test has run yet.

## Scope and boundaries

Implement a reviewable local/staging integration with the company's existing Stripe account **using a sandbox/test environment**. Discover the website's actual checkout implementation, account ownership, API version, customer/payment-method creation, authentication, webhooks and configuration; do not assume it uses the same PaymentIntent flow as the library.

Do not switch the production website's keys or checkout behavior, deploy to the public site, or run live-money test charges. This handoff authorizes integration code and sandbox testing. Use existing secure credential provisioning; never paste secret keys into chats, source, reports or logs. Verify the sandbox key is `sk_test_` before enabling the opt-in test. If no sandbox credentials are available, finish all independent integration work, identify the exact missing configuration, and return that blocker without claiming a successful provider test.

Preserve existing unrelated company-repository changes. Inspect its current branch, status and local instructions before edits; use an isolated checkout/branch where appropriate. Do not fetch/reset over local work or silently upgrade the company's existing dependency environment.

## Retrieve and consume

1. Verify the source repository remote and revision. The local source is available at `C:\code\PrismAgenticPay`; use `git fetch origin` and inspect the pinned implementation commit. If using a new clone, clone the Git URL above into an isolated working directory. Do not copy credentials or the source's local SQLite database.
2. Create an isolated Python 3.11+ environment (tested with 3.12). Install `requirements-tested.txt`, then install the verified checkout with `pip install --no-deps -e <checkout-path>`. For a Git dependency, pin the full implementation SHA rather than floating `main`, and record the consumed SHA.
3. Run the offline suite before integrating. Existing local interpreter: `C:\code\PrismAgenticPay\.venv\Scripts\python.exe`. The reproducible local end-to-end scenario is `tests/test_local_scenario.py` and recovery scenarios are `tests/test_durable_workflow.py`.
4. Prefer an isolated Python service for the company website if its backend is not Python. Keep API/Stripe secrets server-side. Do not claim npm/browser import compatibility for this Python package.

## Integrate the actual company flow

- Use `/v1/authorize/ap2`; raw `/v1/authorize` and `/v1/settle` return 403 in production mode.
- Build an explicit signed test mandate/checkout fixture for the scenario, or consume a genuinely trusted existing mandate issuer if the company already has one. An ordinary website checkout is not automatically an AP2 mandate; report any adapter you add and its trust boundary.
- Configure server-owned identity, authority and public-key registries; example shapes are in `PRODUCTION.md`. Use temporary sandbox identities and fresh authoritative test facts. Separate purchasing and reviewer subjects/keys. Never let browser-submitted authority values decide approval.
- Use a stable test signing seed and an isolated durable SQLite file. The service derives a stable receipt key and refuses a changed deployment signing key for an existing database.
- The supported path is single-currency Stripe. Coinbase/ISO settlement and caller-supplied FX are disabled. Preserve the configured company currency and reject unsupported cross-currency requests.
- Use Stripe payment-method tokens (`pm_...`), not card data. Map the company's actual checkout lifecycle to authorization/review/capture/refund, ensuring it cannot independently charge the same order through both old and new paths. Keep normal production traffic outside this experimental path.
- Each logical refund must have a stable operation ID reused only for retries of that same refund. Distinct equal-amount refunds require distinct IDs.

## Test against the company's Stripe sandbox

With the company's test key securely set in `STRIPE_API_KEY`, set `PAP_RUN_STRIPE_SANDBOX=1` only for the test invocation and run:

```powershell
<python> -m pytest tests/test_stripe_sandbox.py -v
```

This calls real Stripe sandbox APIs: $1 capture, provider evidence verification, refund, and refund evidence verification. The test refuses live keys. It is a provider contract test, not proof that the company website integration is complete.

Then exercise the company local/staging checkout itself and retain redacted evidence for:

1. Successful signed mandate -> policy authorization -> one capture -> signed receipt.
2. REVIEW blocks capture; the purchasing identity cannot impersonate an approver; a distinct authenticated reviewer can approve.
3. Blocked/stale authority cannot reach a charge; fresh server facts can resume GATHER.
4. Tampered proposal/signature, wrong identity/currency, expired mandate and oversized capture are rejected.
5. Partial capture and refund reconcile exactly; repeated capture/refund requests cannot create duplicate financial effects.
6. Two same-amount refunds with different operation IDs are accounted for separately, within the remaining captured amount.
7. Restart after payment/provider-response interruption: the operation remains reserved and can be retried/reconciled without a second charge. Provider idempotency keys are time bounded; retries stop after 23 hours, while read-only reconciliation remains available.
8. Audit events, reviews, operation references, receipt verification and balances survive restart.
9. Existing company checkout tests still pass and live configuration remains unchanged.

If Stripe rejects an account-specific confirmation parameter, API version, payment method or refund status, report the exact sanitized response and request shape. Fix integration code within the assigned scope and rerun affected tests. Do not weaken signature, amount, identity or provider-evidence checks just to make a test green.

## Return handoff

Write `docs/PRISMAGENTICPAY_STRIPE_RETURN.md` in the company repository containing:

- Company repository/branch/revision and the exact PrismAgenticPay SHA consumed.
- Integration files, architecture and checkout mapping; local/staging run commands.
- Environment-variable names and configuration paths only (no values/secrets).
- Stripe sandbox confirmation (never live), executed test commands and outcomes; distinguish pass/fail/skipped/not run.
- Sanitized PaymentIntent/refund/operation references and balance/receipt evidence sufficient to correlate test results.
- Restart/retry/reconciliation results and remaining defects, ordered by severity.
- Any proposed upstream PrismAgenticPay fixes, with a reviewable diff/commit. Do not silently modify the source checkout without reporting it.
- Whether the integration is ready for a deployment review; do not label local success as production certification.

Send a concise return message to source task `01a082bd-c186-7640-9244-b132f3a72a03` using `send_message_to_thread`, host `local`, including the absolute report path, company change references, tested PrismAgenticPay SHA, test results and blockers. Ask that task to review the return report and handle any upstream fixes. If task messaging is unavailable, provide the report path and a copyable return message to the user. Do not leave the outcome only in the receiving task.
