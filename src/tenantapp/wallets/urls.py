from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TenantViewSet, TransferView, UserViewSet, WalletViewSet

router = DefaultRouter()
router.register(r"tenants", TenantViewSet, basename="tenant")
router.register(r"users", UserViewSet, basename="user")
router.register(r"wallets", WalletViewSet, basename="wallet")

urlpatterns = [
    path("", include(router.urls)),
    path("transfers/", TransferView.as_view(), name="transfer"),
]
