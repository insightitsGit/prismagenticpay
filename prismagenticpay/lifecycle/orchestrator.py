"""Durable payment operations: prepare, call provider, atomically finalize.

Unknown outcomes retain their reservation. Recovery reuses the same provider
operation ID and never starts a new charge for an expired idempotency window.
"""
from datetime import datetime, timedelta
import hashlib
import threading
import uuid
from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
from prismagenticpay.core.clock import SystemClock
from prismagenticpay.domain.models import AuthorizationDecision, PaymentProposal
from prismagenticpay.rails.base import RailResult
from prismagenticpay.receipts.ap2 import PaymentReceipt, ReceiptIssuer
from prismagenticpay.state.audit import AuditEvent, DurableAuditLog
from prismagenticpay.state.ledger import HoldStatus


class RailOrchestrator:
    def __init__(self, ledger, gateway, rails, signer, *, receipt_key=None, audit=None, clock=None):
        self.ledger, self.gateway, self.rails, self.signer = ledger, gateway, rails, signer
        self.receipts = ReceiptIssuer(receipt_key or generate_private_key(SECP256R1()))
        self.audit = audit or DurableAuditLog(ledger)
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()

    def _reject(self, rail, detail):
        return RailResult(ok=False, rail_id=rail, reference="", detail=detail)

    def capture(self, proposal, decision, *, rail_id, payment_token, amount_cents=None, now=None):
        current = now or self.clock.now()
        amount = proposal.amount_cents if amount_cents is None else amount_cents
        if (not self.signer.verify(decision) or rail_id not in self.gateway.allowed_rails
                or rail_id not in self.rails or proposal.compute_canonical_payment_hash() != decision.payment_hash
                or proposal.mandate_hash != decision.mandate_hash):
            return self._reject(rail_id, "invalid_capture_authorization"), None
        key = "capture_" + (decision.reservation_id or decision.authorization_id)
        with self._lock:
            with self.ledger.transaction():
                op = self.ledger.get_record("operations", key)
                if op:
                    if op["amount"] != amount or op["rail"] != rail_id or op["token"] != payment_token:
                        return self._reject(rail_id, "operation_parameters_changed"), None
                    if op["status"] in {"done", "failed"}:
                        return self._reject(rail_id, "capture_is_terminal"), None
                else:
                    hold = self.ledger.get(decision.reservation_id or "")
                    if (not decision.is_valid_for_settlement(decision.payment_hash, now=current)
                            or hold is None or hold.status is not HoldStatus.PENDING
                            or hold.payment_hash != decision.payment_hash
                            or not 0 < amount <= min(hold.amount_cents, proposal.amount_cents)):
                        return self._reject(rail_id, "invalid_capture_hold_or_amount"), None
                    if rail_id == "stripe":
                        from prismagenticpay.rails.stripe_rail import _reject_pan
                        _reject_pan(payment_token)
                    self.ledger.start_capture(hold.reservation_id, current)
                    op = self._new(key, "capture", proposal, decision, rail_id, amount, current, payment_token)
                    self.ledger.put_record("operations", key, op)
            return self._run(key, current)

    def refund(self, proposal, decision, *, rail_id, amount_cents, operation_id=None, now=None):
        current = now or self.clock.now()
        if (not operation_id or not self.signer.verify(decision) or rail_id not in self.gateway.allowed_rails
                or rail_id not in self.rails or decision.payment_hash != proposal.compute_canonical_payment_hash()
                or proposal.mandate_hash != decision.mandate_hash):
            return self._reject(rail_id, "invalid_refund_authorization_or_operation_id")
        key = "refund_" + hashlib.sha256((decision.authorization_id + ":" + operation_id).encode()).hexdigest()
        with self._lock:
            with self.ledger.transaction():
                op = self.ledger.get_record("operations", key)
                if op:
                    if op["amount"] != amount_cents or op["rail"] != rail_id:
                        return self._reject(rail_id, "operation_parameters_changed")
                    if op["status"] == "done":
                        return RailResult.model_validate(op["result"])
                else:
                    hold = self.ledger.get(decision.reservation_id or "")
                    capture = self.ledger.get_record("operations", "capture_" + (decision.reservation_id or ""))
                    pending = sum(o["amount"] for o in self.ledger.list_records("operations")
                                  if o["kind"] == "refund" and o["reservation"] == decision.reservation_id and o["status"] == "pending")
                    if (hold is None or hold.status is not HoldStatus.SETTLED or hold.payment_hash != decision.payment_hash
                            or not 0 < amount_cents <= hold.captured_cents - hold.refunded_cents - pending
                            or not capture or capture["status"] != "done" or capture["rail"] != rail_id):
                        return self._reject(rail_id, "invalid_refund_hold_or_amount")
                    op = self._new(key, "refund", proposal, decision, rail_id, amount_cents, current)
                    op["capture_reference"] = capture["reference"]
                    self.ledger.put_record("operations", key, op)
            return self._run(key, current)[0]

    def _new(self, key, kind, proposal, decision, rail, amount, now, token=""):
        return dict(id=key, kind=kind, proposal=proposal.model_dump(mode="json"),
                    decision=decision.model_dump(mode="json"), reservation=decision.reservation_id,
                    rail=rail, amount=amount, created_at=now.isoformat(), lease_until=now.isoformat(),
                    token=token, status="pending", reference="", capture_reference="")

    def _run(self, key, now):
        with self.ledger.transaction():
            op = self.ledger.get_record("operations", key)
            if op is None:
                return self._reject("", "operation_not_found"), None
            if op["rail"] not in self.gateway.allowed_rails or op["rail"] not in self.rails:
                return self._reject(op["rail"], "rail_unavailable"), None
            if op["status"] == "failed":
                return self._reject(op["rail"], "operation_failed"), None
            if op["status"] == "done":
                receipt = PaymentReceipt(op["receipt"], {}) if op.get("receipt") else None
                return RailResult.model_validate(op["result"]), receipt
            if now < datetime.fromisoformat(op["lease_until"]):
                return self._reject(op["rail"], "operation_in_progress"), None
            # Provider idempotency is time bounded. Never risk creating a fresh
            # charge after that window. Operators must reconcile old unknowns.
            if now - datetime.fromisoformat(op["created_at"]) >= timedelta(hours=23):
                return self._reject(op["rail"], "manual_reconciliation_required"), None
            lease = uuid.uuid4().hex
            op.update(lease=lease, lease_until=(now + timedelta(seconds=120)).isoformat())
            self.ledger.put_record("operations", key, op)
        rail = self.rails[op["rail"]]
        proposal = PaymentProposal.model_validate(op["proposal"])
        decision = AuthorizationDecision.model_validate(op["decision"])
        try:
            kwargs = {"operation_id": key} if getattr(rail, "supports_recovery", False) is True else {}
            if op["kind"] == "capture":
                if kwargs:
                    kwargs["capture_reference"] = op["reference"]
                result = rail.capture(proposal, decision, op["amount"], op["token"], **kwargs)
            else:
                if kwargs:
                    kwargs["refund_reference"] = op["reference"]
                result = rail.refund(proposal, decision, op["amount"], op["capture_reference"], **kwargs)
        except Exception:
            # No secrets or provider payloads in durable error records.
            result = self._reject(op["rail"], "provider_outcome_unknown")
        with self.ledger.transaction():
            latest = self.ledger.get_record("operations", key)
            if latest["status"] == "done":
                return RailResult.model_validate(latest["result"]), None
            if latest.get("lease") != lease:
                return self._reject(op["rail"], "operation_lease_changed"), None
            latest["lease_until"] = now.isoformat()
            if result.reference:
                latest["reference"] = result.reference
            if not result.ok:
                latest["last_error"] = result.detail
                self.ledger.put_record("operations", key, latest)
                self._audit("rail.pending", decision, key)
                return result, None
            receipt = None
            if op["kind"] == "capture":
                self.ledger.complete_capture(op["reservation"], op["amount"])
                receipt = self.receipts.issue(proposal, decision, rail_id=op["rail"], rail_reference=result.reference,
                                              outcome="captured", now=now, amount_cents=op["amount"])
            else:
                ok, message = self.ledger.refund(op["reservation"], op["amount"], now=now)
                if not ok:
                    raise RuntimeError(message)
            latest.update(status="done", result=result.model_dump(), receipt=receipt.token if receipt else None)
            self.ledger.put_record("operations", key, latest)
            self._audit("rail." + op["kind"], decision, key)
            return result, receipt

    def recover(self, operation_id, now=None):
        with self._lock:
            return self._run(operation_id, now or self.clock.now())

    def reconcile(self, operation_id, reference=None, now=None):
        """Read provider evidence; this path never creates a payment or refund."""
        current = now or self.clock.now()
        with self._lock:
            with self.ledger.transaction():
                op = self.ledger.get_record("operations", operation_id)
                if not op or op["rail"] not in self.gateway.allowed_rails:
                    return self._reject("", "operation_unavailable")
                if op["status"] != "pending" or current < datetime.fromisoformat(op["lease_until"]):
                    return self._reject(op["rail"], "operation_not_reconcilable")
                rail = self.rails.get(op["rail"])
                if getattr(rail, "supports_recovery", False) is not True:
                    return self._reject(op["rail"], "reconciliation_not_supported")
                lease = uuid.uuid4().hex
                op.update(lease=lease, lease_until=(current + timedelta(seconds=120)).isoformat())
                self.ledger.put_record("operations", operation_id, op)
            decision = AuthorizationDecision.model_validate(op["decision"])
            proposal = PaymentProposal.model_validate(op["proposal"])
            try:
                result = rail.reconcile(proposal, decision, op["amount"], operation_id=operation_id,
                    kind=op["kind"], reference=reference or op["reference"], capture_reference=op["capture_reference"])
            except Exception:
                result = self._reject(op["rail"], "provider_evidence_unavailable_or_mismatched")
            with self.ledger.transaction():
                latest = self.ledger.get_record("operations", operation_id)
                if latest.get("lease") != lease:
                    return self._reject(op["rail"], "operation_lease_changed")
                latest["lease_until"] = current.isoformat()
                if result.ok:
                    receipt = None
                    if op["kind"] == "capture":
                        self.ledger.complete_capture(op["reservation"], op["amount"])
                        receipt = self.receipts.issue(proposal, decision, rail_id=op["rail"], rail_reference=result.reference,
                            outcome="captured", now=current, amount_cents=op["amount"])
                    else:
                        ok, msg = self.ledger.refund(op["reservation"], op["amount"], now=current)
                        if not ok:
                            raise RuntimeError(msg)
                    latest.update(status="done", reference=result.reference, result=result.model_dump(), receipt=receipt.token if receipt else None)
                elif result.detail == "confirmed_not_paid":
                    if op["kind"] == "capture":
                        self.ledger.cancel_capture(op["reservation"])
                    latest.update(status="failed", reference=result.reference, result=result.model_dump())
                self.ledger.put_record("operations", operation_id, latest)
                self._audit("rail.reconciled", decision, operation_id + ":" + result.detail)
                return result

    def void(self, decision, *, rail_id):
        # Local uncaptured authorization can be abandoned; remote unknown
        # operations require reconciliation, not a speculative cancellation.
        with self.ledger.transaction():
            if not self.signer.verify(decision) or rail_id not in self.gateway.allowed_rails:
                return self._reject(rail_id, "invalid_void_authorization")
            hold = self.ledger.get(decision.reservation_id or "")
            if hold is None or hold.status is not HoldStatus.PENDING:
                return self._reject(rail_id, "operation_requires_reconciliation")
            self.ledger.release(hold.reservation_id)
            self._audit("rail.void", decision, hold.reservation_id)
            return RailResult(ok=True, rail_id=rail_id, reference="", detail="local_hold_released")

    def _audit(self, event_type, decision, detail):
        self.audit.append(AuditEvent(event_id="aud_" + uuid.uuid4().hex, event_type=event_type,
            authorization_id=decision.authorization_id, payment_hash=decision.payment_hash, actor_id="rail", detail=detail))
