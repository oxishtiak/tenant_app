from rest_framework import serializers

from .models import Tenant, TenantUser, Transaction, Wallet


class TenantCreateSerializer(serializers.ModelSerializer):
    api_key = serializers.CharField(read_only=True)

    class Meta:
        model = Tenant
        fields = ["id", "name", "api_key", "created_at"]
        read_only_fields = ["id", "api_key", "created_at"]


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantUser
        fields = ["id", "email", "display_name", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_email(self, value):
        return value.strip().lower()


class WalletSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    user_id = serializers.UUIDField(write_only=True)

    class Meta:
        model = Wallet
        fields = ["id", "user", "user_id", "balance", "created_at"]
        read_only_fields = ["id", "user", "balance", "created_at"]


class MoneySerializer(serializers.Serializer):
    amount = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(min_length=1, max_length=64)


class TransferSerializer(serializers.Serializer):
    from_wallet = serializers.UUIDField()
    to_wallet = serializers.UUIDField()
    amount = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(min_length=1, max_length=64)

    def validate(self, attrs):
        if attrs["from_wallet"] == attrs["to_wallet"]:
            raise serializers.ValidationError("from_wallet and to_wallet must differ.")
        return attrs


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = [
            "id",
            "tx_type",
            "amount",
            "balance_after",
            "counterpart_wallet",
            "created_at",
        ]
