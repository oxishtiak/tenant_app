"""Data model: Tenant -> User -> Wallet -> immutable Transaction ledger."""
import secrets
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


class Tenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    api_key = models.CharField(max_length=64, unique=True, default=generate_api_key, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.pk})"


class TenantUser(models.Model):
    """End user belonging to exactly one tenant. Owns a single wallet."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="users")
    email = models.EmailField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "email"], name="uniq_user_email_per_tenant"),
        ]
        indexes = [models.Index(fields=["tenant", "email"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.email} [{self.tenant_id}]"


class Wallet(models.Model):
    """Holds a cached balance (minor units) guarded by row locks; Transaction rows are truth."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="wallets")
    user = models.OneToOneField(TenantUser, on_delete=models.CASCADE, related_name="wallet")
    balance = models.BigIntegerField(default=0, validators=[MinValueValidator(0)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(balance__gte=0),
                name="wallet_balance_non_negative",
            )
        ]
        indexes = [models.Index(fields=["tenant", "user"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Wallet {self.pk} user={self.user_id} bal={self.balance}"


class Transaction(models.Model):
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    TYPES = (
        (DEPOSIT, "Deposit"),
        (WITHDRAW, "Withdraw"),
        (TRANSFER_IN, "Transfer in"),
        (TRANSFER_OUT, "Transfer out"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="transactions")
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="transactions")
    tx_type = models.CharField(max_length=16, choices=TYPES, db_index=True)
    amount = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    balance_after = models.BigIntegerField()
    counterpart_wallet = models.ForeignKey(
        Wallet, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "wallet", "-created_at"])]
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        # Ledger is append-only: forbid updates without an extra query.
        # UUID pk is assigned on instantiation, so check _state.adding instead of self.pk.
        if not self._state.adding:
            raise ValidationError("Transactions are immutable.")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.tx_type} {self.amount} wallet={self.wallet_id}"


class IdempotencyRecord(models.Model):
    """Stores the replayable response for a (tenant, idempotency_key) pair.

    One row per mutating money request. The unique constraint plus catching
    IntegrityError under concurrency is what makes retries safe.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="idempotency_records")
    key = models.CharField(max_length=64, db_index=True)
    status_code = models.PositiveSmallIntegerField(default=200)
    response = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "key"], name="uniq_idem_key_per_tenant"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"idem {self.key} tenant={self.tenant_id}"
