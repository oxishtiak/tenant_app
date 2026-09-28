from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .authentication import TenantAPIKeyAuthentication
from .models import Tenant, TenantUser, Transaction, Wallet
from .permissions import HasTenantScope
from .serializers import (
    MoneySerializer,
    TenantCreateSerializer,
    TransactionSerializer,
    TransferSerializer,
    UserSerializer,
    WalletSerializer,
)


class TenantViewSet(viewsets.GenericViewSet):
    queryset = Tenant.objects.all()
    serializer_class = TenantCreateSerializer
    authentication_classes = []
    permission_classes = [AllowAny]
    allow_any_tenant = True

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = serializer.save()
        return Response(self.get_serializer(tenant).data, status=status.HTTP_201_CREATED)


class UserViewSet(viewsets.ModelViewSet):
    serializer_class = UserSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return TenantUser.objects.filter(tenant=self.request.tenant).order_by("-created_at")

    def perform_create(self, serializer):
        try:
            serializer.save(tenant=self.request.tenant)
        except IntegrityError:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"email": "User with this email already exists for this tenant."})


class WalletViewSet(viewsets.ModelViewSet):
    serializer_class = WalletSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return (
            Wallet.objects.filter(tenant=self.request.tenant)
            .select_related("user")
            .order_by("-created_at")
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # user_id must belong to the requesting tenant (404 otherwise -> no leak).
        user = get_object_or_404(TenantUser, pk=serializer.validated_data["user_id"], tenant=request.tenant)
        if hasattr(user, "wallet"):
            return Response(
                {"detail": "User already has a wallet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        wallet = Wallet.objects.create(tenant=request.tenant, user=user)
        return Response(self.get_serializer(wallet).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="deposit")
    def deposit(self, request, pk=None):
        wallet = get_object_or_404(Wallet, pk=pk, tenant=request.tenant)
        serializer = MoneySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return services.deposit(
            tenant=request.tenant,
            wallet=wallet,
            amount=serializer.validated_data["amount"],
            idempotency_key=serializer.validated_data["idempotency_key"],
        )

    @action(detail=True, methods=["post"], url_path="withdraw")
    def withdraw(self, request, pk=None):
        wallet = get_object_or_404(Wallet, pk=pk, tenant=request.tenant)
        serializer = MoneySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return services.withdraw(
            tenant=request.tenant,
            wallet=wallet,
            amount=serializer.validated_data["amount"],
            idempotency_key=serializer.validated_data["idempotency_key"],
        )

    @action(detail=True, methods=["get"], url_path="transactions")
    def transactions(self, request, pk=None):
        wallet = get_object_or_404(Wallet, pk=pk, tenant=request.tenant)
        qs = Transaction.objects.filter(tenant=request.tenant, wallet=wallet).order_by("-created_at")
        page = self.paginate_queryset(qs)
        serializer = TransactionSerializer(page if page is not None else qs, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class TransferView(APIView):
    authentication_classes = [TenantAPIKeyAuthentication]
    permission_classes = [HasTenantScope]

    def post(self, request):
        serializer = TransferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        from_wallet = get_object_or_404(Wallet, pk=data["from_wallet"], tenant=request.tenant)
        to_wallet = get_object_or_404(Wallet, pk=data["to_wallet"], tenant=request.tenant)
        return services.transfer(
            tenant=request.tenant,
            from_wallet=from_wallet,
            to_wallet=to_wallet,
            amount=data["amount"],
            idempotency_key=data["idempotency_key"],
        )
