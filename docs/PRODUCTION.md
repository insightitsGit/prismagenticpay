# Production operations

PrismAgenticPay is a pre-settlement policy authority. It authorizes agent spend, then a rail moves money.

## What is production-ready

- Signed authorization decisions
- Multi-bucket holds with TTL, capture, partial capture, refund, void
- AP2 closed-mandate verification
- Stripe PaymentIntents using **payment_method tokens only**
- ISO 8583-1987 0100/0110 codec + configured host
- Coinbase Commerce charges
- SAP / NetSuite / Coupa HTTP fact connectors
- Locked FX quotes (no invented rates)
- Audit console and policy simulation
- API keys, rate limits, health/ready checks
- Durable SQLite ledger (put the file on durable disk or a managed volume)

## PCI boundary

This service must never receive a PAN, CVV, or track data. Callers send Stripe `payment_method` tokens or switch tokens. Card data stays at the rail. That keeps this process out of PCI CHD storage.

## Required production env

- `PAP_ENV=production`
- `PAP_API_KEYS` — comma-separated long random keys
- `PAP_SIGNING_SEED_HEX` — 32-byte Ed25519 seed, hex-encoded
- `PAP_DATABASE_URL=sqlite:////var/lib/prismagenticpay/ledger.sqlite`
- Rail keys only for the rails you enable

Terminate TLS at the load balancer. Do not expose the console without the API key header.

## Honest limits

- Coinbase Commerce has no refund/void API; those calls fail closed.
- ISO 8583 talks to **your** switch URL. We are not a card network.
- ERP adapters expect the JSON paths documented in each connector. Map your tenant fields there.
- Multi-region active-active is not included. Run one writer plus storage failover.
