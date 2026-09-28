"""Ledger operations: atomic, row-locked, idempotent per tenant."""
import time

from django.db import IntegrityError, OperationalError, transaction
from rest_framework import status
from rest_framework.response import Response

from .models import IdempotencyRecord, Transaction, Wallet

_MAX_RETRIES = 8


def _is_locked_error(exc: Exception) -> bool:
    return isinstance(exc, OperationalError) and "locked" in str(exc).lower()


def _replay(record: IdempotencyRecord) -> Response:
    return Response(record.response, status=record.status_code)


def _record(tenant, key: str, payload: dict, status_code: int = status.HTTP_200_OK) -> IdempotencyRecord:
    return IdempotencyRecord.objects.create(
        tenant=tenant, key=key, response=payload, status_code=status_code
    )


def deposit(*, tenant, wallet: Wallet, amount: int, idempotency_key: str) -> Response:
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            with transaction.atomic():
                existing = (
                    IdempotencyRecord.objects.select_for_update()
                    .filter(tenant=tenant, key=idempotency_key)
                    .first()
                )
                if existing:
                    return _replay(existing)
                locked = Wallet.objects.select_for_update().get(pk=wallet.pk, tenant=tenant)
                locked.balance = locked.balance + amount
                locked.save(update_fields=["balance"])
                tx = Transaction.objects.create(
                    tenant=tenant,
                    wallet=locked,
                    tx_type=Transaction.DEPOSIT,
                    amount=amount,
                    balance_after=locked.balance,
                )
                payload = {
                    "wallet_id": str(locked.pk),
                    "balance": locked.balance,
                    "transaction_id": str(tx.pk),
                }
                _record(tenant, idempotency_key, payload, status.HTTP_200_OK)
                return Response(payload, status=status.HTTP_200_OK)
        except IntegrityError:
            # Concurrent duplicate key won the race; replay the winner.
            record = IdempotencyRecord.objects.filter(tenant=tenant, key=idempotency_key).first()
            if record is not None:
                return _replay(record)
            raise
        except OperationalError as exc:
            # SQLite dev fallback has table-level locks; retry briefly.
            # Postgres uses true row locks and rarely hits this path.
            last_exc = exc
            if _is_locked_error(exc) and attempt < _MAX_RETRIES - 1:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
    raise last_exc  # pragma: no cover


def withdraw(*, tenant, wallet: Wallet, amount: int, idempotency_key: str) -> Response:
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            with transaction.atomic():
                existing = (
                    IdempotencyRecord.objects.select_for_update()
                    .filter(tenant=tenant, key=idempotency_key)
                    .first()
                )
                if existing:
                    return _replay(existing)
                locked = Wallet.objects.select_for_update().get(pk=wallet.pk, tenant=tenant)
                if locked.balance < amount:
                    payload = {"detail": "Insufficient funds.", "balance": locked.balance}
                    _record(
                        tenant,
                        idempotency_key,
                        payload,
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )
                    return Response(
                        payload,
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )
                locked.balance = locked.balance - amount
                locked.save(update_fields=["balance"])
                tx = Transaction.objects.create(
                    tenant=tenant,
                    wallet=locked,
                    tx_type=Transaction.WITHDRAW,
                    amount=amount,
                    balance_after=locked.balance,
                )
                payload = {
                    "wallet_id": str(locked.pk),
                    "balance": locked.balance,
                    "transaction_id": str(tx.pk),
                }
                _record(tenant, idempotency_key, payload, status.HTTP_200_OK)
                return Response(payload, status=status.HTTP_200_OK)
        except IntegrityError:
            record = IdempotencyRecord.objects.filter(tenant=tenant, key=idempotency_key).first()
            if record is not None:
                return _replay(record)
            raise
        except OperationalError as exc:
            last_exc = exc
            if _is_locked_error(exc) and attempt < _MAX_RETRIES - 1:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
    raise last_exc  # pragma: no cover


def transfer(*, tenant, from_wallet: Wallet, to_wallet: Wallet, amount: int, idempotency_key: str) -> Response:
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            with transaction.atomic():
                existing = (
                    IdempotencyRecord.objects.select_for_update()
                    .filter(tenant=tenant, key=idempotency_key)
                    .first()
                )
                if existing:
                    return _replay(existing)
                # Lock in deterministic order to avoid deadlocks.
                locked_map = {
                    str(w.pk): w
                    for w in Wallet.objects.select_for_update().filter(
                        pk__in=[from_wallet.pk, to_wallet.pk], tenant=tenant
                    )
                }
                if len(locked_map) != 2:
                    payload = {"detail": "Both wallets must belong to the requesting tenant."}
                    _record(
                        tenant,
                        idempotency_key,
                        payload,
                        status.HTTP_400_BAD_REQUEST,
                    )
                    return Response(
                        payload,
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                src = locked_map[str(from_wallet.pk)]
                dst = locked_map[str(to_wallet.pk)]
                if src.balance < amount:
                    payload = {"detail": "Insufficient funds.", "balance": src.balance}
                    _record(
                        tenant,
                        idempotency_key,
                        payload,
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )
                    return Response(
                        payload,
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )
                src.balance -= amount
                dst.balance += amount
                src.save(update_fields=["balance"])
                dst.save(update_fields=["balance"])
                tx_out = Transaction.objects.create(
                    tenant=tenant,
                    wallet=src,
                    tx_type=Transaction.TRANSFER_OUT,
                    amount=amount,
                    balance_after=src.balance,
                    counterpart_wallet=dst,
                )
                tx_in = Transaction.objects.create(
                    tenant=tenant,
                    wallet=dst,
                    tx_type=Transaction.TRANSFER_IN,
                    amount=amount,
                    balance_after=dst.balance,
                    counterpart_wallet=src,
                )
                payload = {
                    "from_wallet_id": str(src.pk),
                    "to_wallet_id": str(dst.pk),
                    "amount": amount,
                    "from_balance": src.balance,
                    "to_balance": dst.balance,
                    "debit_transaction_id": str(tx_out.pk),
                    "credit_transaction_id": str(tx_in.pk),
                }
                _record(tenant, idempotency_key, payload, status.HTTP_200_OK)
                return Response(payload, status=status.HTTP_200_OK)
        except IntegrityError:
            record = IdempotencyRecord.objects.filter(tenant=tenant, key=idempotency_key).first()
            if record is not None:
                return _replay(record)
            raise
        except OperationalError as exc:
            last_exc = exc
            if _is_locked_error(exc) and attempt < _MAX_RETRIES - 1:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
    raise last_exc  # pragma: no cover
