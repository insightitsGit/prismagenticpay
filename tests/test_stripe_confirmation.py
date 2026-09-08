from urllib.parse import parse_qs

import httpx
import pytest

from conftest import fresh_metadata
from prismagenticpay.config import Settings
from prismagenticpay.rails.stripe_rail import StripeRail, StripeRailError


@pytest.mark.parametrize("return_url", ["", "https://merchant.example/payment/return"])
def test_confirmation_is_card_only_and_preserves_retry_parameters(
    proposal, authorizer, authority, frozen_now, return_url
):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/payment_intents":
            fields = parse_qs(request.content.decode())
            # Reproduce the account contract: unspecified methods require a return URL.
            assert fields["payment_method_types[0]"] == ["card"]
            assert fields.get("return_url", []) == ([return_url] if return_url else [])
            assert fields["capture_method"] == ["manual"]
            return httpx.Response(200, json={"id": "pi_test", "status": "requires_capture"})
        return httpx.Response(200, json={"id": "pi_test", "status": "succeeded"})

    decision = authorizer.process_authorization(
        proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now)
    )
    rail = StripeRail("sk_test_fixture", transport=httpx.MockTransport(handler), return_url=return_url)
    try:
        for _ in range(2):
            assert rail.capture(proposal, decision, 100, "pm_card_visa", operation_id="retry1").ok
        creates = [r for r in requests if r.url.path == "/v1/payment_intents"]
        assert creates[0].content == creates[1].content
        assert creates[0].headers["Idempotency-Key"] == creates[1].headers["Idempotency-Key"]
    finally:
        rail._client.close()


@pytest.mark.parametrize("url", ["/relative", "javascript:alert(1)", "https://user:secret@example.com", "https://"])
def test_invalid_return_url_fails_before_network(url):
    with pytest.raises(StripeRailError, match="return_url"):
        StripeRail("sk_test_fixture", return_url=url)


def test_return_url_environment(monkeypatch):
    monkeypatch.setenv("STRIPE_RETURN_URL", "https://merchant.example/return")
    assert Settings.from_env().stripe_return_url == "https://merchant.example/return"
