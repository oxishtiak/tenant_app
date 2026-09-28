"""Tenant resolution: X-API-Key (preferred) or X-Tenant-ID (dev convenience)."""
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import Tenant


class TenantAPIKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        # Public bootstrap endpoint handles its own permissions; skip auth there
        # so missing headers don't 403 tenant creation.
        if request.path.endswith("/tenants/") and request.method == "POST":
            return None

        api_key = request.headers.get("X-Api-Key") or request.headers.get("X-API-Key")
        tenant_id = request.headers.get("X-Tenant-Id") or request.headers.get("X-Tenant-ID")

        tenant = None
        if api_key:
            try:
                tenant = Tenant.objects.get(api_key=api_key)
            except Tenant.DoesNotExist:
                raise AuthenticationFailed("Invalid API key.")
        elif tenant_id:
            try:
                tenant = Tenant.objects.get(pk=tenant_id)
            except (Tenant.DoesNotExist, ValueError, TypeError):
                raise AuthenticationFailed("Invalid tenant ID.")
        else:
            raise AuthenticationFailed("Missing X-API-Key or X-Tenant-ID header.")

        request.tenant = tenant
        # DRF expects (user, auth); Tenant acts as the "user" for permission checks.
        return (tenant, None)

    def authenticate_header(self, request):
        return "X-API-Key"
