from rest_framework.permissions import BasePermission


class HasTenantScope(BasePermission):
    """All non-bootstrap endpoints require a resolved tenant."""

    message = "Tenant scope required."

    def has_permission(self, request, view):
        # Tenant creation is public.
        if getattr(view, "allow_any_tenant", False):
            return True
        return getattr(request, "tenant", None) is not None
