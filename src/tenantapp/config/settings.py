"""Django settings for multi-tenant wallet API."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-secret-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "tenantapp.wallets",
]

MIDDLEWARE: list = []

ROOT_URLCONF = "tenantapp.config.urls"
WSGI_APPLICATION = "tenantapp.config.wsgi.application"

# Database: PostgreSQL via DATABASE_URL-style env vars, else SQLite fallback.
# Preferred (Postgres): POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_PORT
# or a full DATABASE_URL is intentionally not parsed to avoid extra deps.
if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "tenantapp"),
            "USER": os.environ.get("POSTGRES_USER", "tenantapp"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "tenantapp"),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "ATOMIC_REQUESTS": False,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "tenantapp.wallets.authentication.TenantAPIKeyAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "tenantapp.wallets.permissions.HasTenantScope",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}
