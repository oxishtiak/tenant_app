"""Full verification suite for Multi-Tenant Wallet API.

Covers project/evaluation_criteria.md critical cases plus overall working
procedure: tenant bootstrap -> users/wallets -> deposit/withdraw/transfer ->
balance + paginated history, with tenant isolation, ledger integrity,
idempotency (per-tenant, under retries) and concurrency (select_for_update).
"""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from django.core.exceptions import ValidationError
from django.db import connections
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from .models import IdempotencyRecord, Tenant, TenantUser, Transaction, Wallet


def make_tenant(name="Acme"):
    return Tenant.objects.create(name=name)


def auth_key(client: APIClient, tenant: Tenant):
    client.credentials(HTTP_X_API_KEY=tenant.api_key)


def auth_tenant_id(client: APIClient, tenant: Tenant):
    client.credentials(HTTP_X_TENANT_ID=str(tenant.pk))


def create_user_wallet(client: APIClient, email: str):
    r = client.post("/api/users/", {"email": email}, format="json")
    assert r.status_code == 201, r.content
    user_id = r.data["id"]
    r = client.post("/api/wallets/", {"user_id": user_id}, format="json")
    assert r.status_code == 201, r.content
    return user_id, r.data["id"]


class TenantBootstrapTests(TestCase):
    def test_create_tenant_is_public(self):
        c = APIClient()  # no credentials
        r = c.post("/api/tenants/", {"name": "NewCo"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertIn("api_key", r.data)
        self.assertIn("id", r.data)

    def test_missing_auth_rejected(self):
        c = APIClient()
        r = c.get("/api/wallets/", format="json")
        self.assertIn(r.status_code, (401, 403))

    def test_invalid_api_key_rejected(self):
        c = APIClient()
        c.credentials(HTTP_X_API_KEY="bad-key")
        r = c.get("/api/wallets/", format="json")
        self.assertIn(r.status_code, (401, 403))

    def test_x_tenant_id_header_works(self):
        tenant = make_tenant()
        c = APIClient()
        auth_tenant_id(c, tenant)
        r = c.post("/api/users/", {"email": "tid@example.com"}, format="json")
        self.assertEqual(r.status_code, 201)


class UserWalletCreationTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()
        self.client = APIClient()
        auth_key(self.client, self.tenant)

    def test_create_user_and_wallet(self):
        user_id, wallet_id = create_user_wallet(self.client, "a@example.com")
        r = self.client.get(f"/api/wallets/{wallet_id}/", format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["balance"], 0)

    def test_duplicate_email_same_tenant_rejected(self):
        self.client.post("/api/users/", {"email": "dup@example.com"}, format="json")
        r = self.client.post("/api/users/", {"email": "dup@example.com"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_same_email_different_tenant_allowed(self):
        self.client.post("/api/users/", {"email": "shared@example.com"}, format="json")
        other = make_tenant("Other")
        c2 = APIClient()
        auth_key(c2, other)
        r = c2.post("/api/users/", {"email": "shared@example.com"}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_duplicate_wallet_for_user_rejected(self):
        r = self.client.post("/api/users/", {"email": "one@example.com"}, format="json")
        user_id = r.data["id"]
        r1 = self.client.post("/api/wallets/", {"user_id": user_id}, format="json")
        self.assertEqual(r1.status_code, 201)
        r2 = self.client.post("/api/wallets/", {"user_id": user_id}, format="json")
        self.assertEqual(r2.status_code, 400)

    def test_wallet_create_with_other_tenant_user_404(self):
        other = make_tenant("Other")
        foreign_user = TenantUser.objects.create(tenant=other, email="f@example.com")
        r = self.client.post("/api/wallets/", {"user_id": str(foreign_user.pk)}, format="json")
        self.assertEqual(r.status_code, 404)

    def test_list_endpoints_are_tenant_scoped(self):
        create_user_wallet(self.client, "mine@example.com")
        other = make_tenant("Other")
        c2 = APIClient()
        auth_key(c2, other)
        create_user_wallet(c2, "theirs@example.com")
        r = self.client.get("/api/users/", format="json")
        emails = [u["email"] for u in r.data["results"]] if "results" in r.data else [u["email"] for u in r.data]
        self.assertIn("mine@example.com", emails)
        self.assertNotIn("theirs@example.com", emails)


class MoneyOperationTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()
        self.client = APIClient()
        auth_key(self.client, self.tenant)

    def test_deposit_and_get_balance(self):
        _, w1 = create_user_wallet(self.client, "d@example.com")
        r = self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 1000, "idempotency_key": "dep-1"}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["balance"], 1000)
        self.assertIsInstance(r.data["balance"], int)
        r = self.client.get(f"/api/wallets/{w1}/", format="json")
        self.assertEqual(r.data["balance"], 1000)

    def test_withdraw_success(self):
        _, w1 = create_user_wallet(self.client, "w@example.com")
        self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 1000, "idempotency_key": "d1"}, format="json"
        )
        r = self.client.post(
            f"/api/wallets/{w1}/withdraw/", {"amount": 300, "idempotency_key": "w1"}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["balance"], 700)
        tx = Transaction.objects.get(wallet_id=w1, tx_type=Transaction.WITHDRAW)
        self.assertEqual(tx.balance_after, 700)

    def test_withdraw_insufficient_funds_rejected(self):
        _, w1 = create_user_wallet(self.client, "poor@example.com")
        self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 100, "idempotency_key": "d1"}, format="json"
        )
        r = self.client.post(
            f"/api/wallets/{w1}/withdraw/", {"amount": 500, "idempotency_key": "w1"}, format="json"
        )
        self.assertEqual(r.status_code, 422)
        wallet = Wallet.objects.get(pk=w1)
        self.assertEqual(wallet.balance, 100)
        self.assertEqual(Transaction.objects.filter(wallet=wallet, tx_type="withdraw").count(), 0)

    def test_transfer_insufficient_funds_rejected(self):
        _, w1 = create_user_wallet(self.client, "t1@example.com")
        _, w2 = create_user_wallet(self.client, "t2@example.com")
        self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 100, "idempotency_key": "seed"}, format="json"
        )
        r = self.client.post(
            "/api/transfers/",
            {"from_wallet": w1, "to_wallet": w2, "amount": 500, "idempotency_key": "x-bad"},
            format="json",
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(Wallet.objects.get(pk=w1).balance, 100)
        self.assertEqual(Wallet.objects.get(pk=w2).balance, 0)
        self.assertEqual(Transaction.objects.filter(wallet_id=w1, tx_type="transfer_out").count(), 0)

    def test_transfer_same_wallet_rejected(self):
        _, w1 = create_user_wallet(self.client, "s@example.com")
        r = self.client.post(
            "/api/transfers/",
            {"from_wallet": w1, "to_wallet": w1, "amount": 10, "idempotency_key": "x-same"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

    def test_money_validation(self):
        _, w1 = create_user_wallet(self.client, "v@example.com")
        for bad in (0, -5):
            r = self.client.post(
                f"/api/wallets/{w1}/deposit/", {"amount": bad, "idempotency_key": f"k-{bad}"}, format="json"
            )
            self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/wallets/{w1}/deposit/", {"amount": 100}, format="json")
        self.assertEqual(r.status_code, 400)  # missing idempotency_key
        r = self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": "not-a-number", "idempotency_key": "k-nan"}, format="json"
        )
        self.assertEqual(r.status_code, 400)

    def test_transfer_and_paginated_history(self):
        _, w1 = create_user_wallet(self.client, "h1@example.com")
        _, w2 = create_user_wallet(self.client, "h2@example.com")
        self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 1000, "idempotency_key": "dep-1"}, format="json"
        )
        r = self.client.post(
            "/api/transfers/",
            {"from_wallet": w1, "to_wallet": w2, "amount": 400, "idempotency_key": "xfer-1"},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["from_balance"], 600)
        self.assertEqual(r.data["to_balance"], 400)
        r = self.client.get(f"/api/wallets/{w1}/transactions/", format="json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("count", r.data)
        self.assertIn("results", r.data)
        self.assertGreaterEqual(r.data["count"], 2)


class LedgerIntegrityTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()
        self.client = APIClient()
        auth_key(self.client, self.tenant)

    def test_ledger_balance_after_matches_wallet(self):
        _, w1 = create_user_wallet(self.client, "l1@example.com")
        _, w2 = create_user_wallet(self.client, "l2@example.com")
        self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 1000, "idempotency_key": "dep-1"}, format="json"
        )
        self.client.post(
            "/api/transfers/",
            {"from_wallet": w1, "to_wallet": w2, "amount": 400, "idempotency_key": "xfer-1"},
            format="json",
        )
        wallet = Wallet.objects.get(pk=w1)
        # Deterministic: latest transfer_out for this wallet is the truth, not
        # order_by(created_at).last() which ties when timestamps collide.
        tx_out = Transaction.objects.filter(wallet=wallet, tx_type=Transaction.TRANSFER_OUT).order_by(
            "-created_at"
        ).first()
        self.assertIsNotNone(tx_out)
        self.assertEqual(tx_out.balance_after, wallet.balance)
        self.assertEqual(wallet.balance, 600)
        # Counterpart linkage both sides.
        tx_in = Transaction.objects.get(wallet_id=w2, tx_type=Transaction.TRANSFER_IN)
        self.assertEqual(tx_in.counterpart_wallet_id, wallet.pk)
        self.assertEqual(tx_out.counterpart_wallet_id, tx_in.wallet_id)

    def test_ledger_is_immutable(self):
        _, w1 = create_user_wallet(self.client, "imm@example.com")
        self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 100, "idempotency_key": "d1"}, format="json"
        )
        tx = Transaction.objects.filter(wallet_id=w1).first()
        tx.amount = 99999
        with self.assertRaises(ValidationError):
            tx.save()

    def test_failed_transfer_leaves_both_sides_unchanged(self):
        _, w1 = create_user_wallet(self.client, "a1@example.com")
        _, w2 = create_user_wallet(self.client, "a2@example.com")
        self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 50, "idempotency_key": "seed"}, format="json"
        )
        before1 = Wallet.objects.get(pk=w1).balance
        before2 = Wallet.objects.get(pk=w2).balance
        n_tx = Transaction.objects.count()
        r = self.client.post(
            "/api/transfers/",
            {"from_wallet": w1, "to_wallet": w2, "amount": 5000, "idempotency_key": "x-fail"},
            format="json",
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(Wallet.objects.get(pk=w1).balance, before1)
        self.assertEqual(Wallet.objects.get(pk=w2).balance, before2)
        # Only the failed-attempt idempotency record exists; no new money rows
        # for the destination. (Failed withdraw/transfer still records the
        # idempotent error payload but must not create transfer_* rows.)
        self.assertEqual(Transaction.objects.filter(tx_type__in=["transfer_in", "transfer_out"]).count(), 0)
        self.assertTrue(
            IdempotencyRecord.objects.filter(tenant=self.tenant, key="x-fail").exists()
        )
        _ = n_tx


class IdempotencyTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()
        self.client = APIClient()
        auth_key(self.client, self.tenant)

    def test_duplicate_idempotency_key_no_double_charge_deposit(self):
        _, w1 = create_user_wallet(self.client, "idem@example.com")
        payload = {"amount": 250, "idempotency_key": "same-key"}
        r1 = self.client.post(f"/api/wallets/{w1}/deposit/", payload, format="json")
        r2 = self.client.post(f"/api/wallets/{w1}/deposit/", payload, format="json")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.data, r2.data)
        wallet = Wallet.objects.get(pk=w1)
        self.assertEqual(wallet.balance, 250)
        self.assertEqual(Transaction.objects.filter(wallet=wallet).count(), 1)

    def test_duplicate_idempotency_key_transfer_replays(self):
        _, w1 = create_user_wallet(self.client, "i1@example.com")
        _, w2 = create_user_wallet(self.client, "i2@example.com")
        self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 500, "idempotency_key": "seed"}, format="json"
        )
        payload = {"from_wallet": w1, "to_wallet": w2, "amount": 200, "idempotency_key": "dup-x"}
        r1 = self.client.post("/api/transfers/", payload, format="json")
        r2 = self.client.post("/api/transfers/", payload, format="json")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.data, r2.data)
        self.assertEqual(Wallet.objects.get(pk=w1).balance, 300)
        self.assertEqual(Wallet.objects.get(pk=w2).balance, 200)
        self.assertEqual(Transaction.objects.filter(tx_type="transfer_out").count(), 1)
        self.assertEqual(Transaction.objects.filter(tx_type="transfer_in").count(), 1)

    def test_idempotency_key_scoped_per_tenant(self):
        _, w1 = create_user_wallet(self.client, "sc1@example.com")
        other = make_tenant("Other")
        c2 = APIClient()
        auth_key(c2, other)
        _, w2 = create_user_wallet(c2, "sc2@example.com")
        r1 = self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 100, "idempotency_key": "shared-key"}, format="json"
        )
        r2 = c2.post(
            f"/api/wallets/{w2}/deposit/", {"amount": 100, "idempotency_key": "shared-key"}, format="json"
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # Different tenants -> different ledger rows, not a replay.
        self.assertNotEqual(r1.data["transaction_id"], r2.data["transaction_id"])
        self.assertEqual(Wallet.objects.get(pk=w1).balance, 100)
        self.assertEqual(Wallet.objects.get(pk=w2).balance, 100)


class CrossTenantIsolationTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()
        self.client = APIClient()
        auth_key(self.client, self.tenant)

    def test_cross_tenant_access_blocked(self):
        _, w1 = create_user_wallet(self.client, "victim@example.com")
        other = make_tenant("Evil")
        evil = APIClient()
        auth_key(evil, other)

        r = evil.get(f"/api/wallets/{w1}/", format="json")
        self.assertEqual(r.status_code, 404)

        r = evil.post(
            f"/api/wallets/{w1}/withdraw/", {"amount": 10, "idempotency_key": "evil"}, format="json"
        )
        self.assertEqual(r.status_code, 404)

        _, evil_wallet = create_user_wallet(evil, "evil@example.com")
        evil.post(
            f"/api/wallets/{evil_wallet}/deposit/", {"amount": 1000, "idempotency_key": "e1"},
            format="json",
        )
        r = evil.post(
            "/api/transfers/",
            {"from_wallet": evil_wallet, "to_wallet": w1, "amount": 100, "idempotency_key": "x1"},
            format="json",
        )
        self.assertIn(r.status_code, (400, 404))
        self.assertEqual(Wallet.objects.get(pk=w1).balance, 0)

    def test_cross_tenant_transactions_history_blocked(self):
        _, w1 = create_user_wallet(self.client, "v2@example.com")
        self.client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 100, "idempotency_key": "d1"}, format="json"
        )
        other = make_tenant("Evil2")
        evil = APIClient()
        auth_key(evil, other)
        r = evil.get(f"/api/wallets/{w1}/transactions/", format="json")
        self.assertEqual(r.status_code, 404)


class ConcurrentTransferTests(TransactionTestCase):
    def test_concurrent_transfers_preserve_total(self):
        tenant = make_tenant()
        client = APIClient()
        auth_key(client, tenant)
        _, w1 = create_user_wallet(client, "c1@example.com")
        _, w2 = create_user_wallet(client, "c2@example.com")
        w1_id, w2_id = str(w1), str(w2)
        client.post(
            f"/api/wallets/{w1}/deposit/", {"amount": 1000, "idempotency_key": "seed"}, format="json"
        )
        api_key = tenant.api_key
        errors = []

        def do_transfer(i: int):
            c = APIClient()
            c.credentials(HTTP_X_API_KEY=api_key)
            try:
                for attempt in range(10):
                    try:
                        r = c.post(
                            "/api/transfers/",
                            {
                                "from_wallet": w1,
                                "to_wallet": w2,
                                "amount": 100,
                                "idempotency_key": f"conc-{i}",
                            },
                            format="json",
                        )
                        if r.status_code not in (200, 422):
                            errors.append((i, r.status_code, r.content[:200]))
                        return
                    except Exception as exc:  # noqa: BLE001 - sqlite lock contention retry
                        if "locked" in str(exc).lower() and attempt < 9:
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        errors.append((i, repr(exc)))
                        return
            finally:
                for conn in connections.all():
                    conn.close()

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(do_transfer, range(10)))

        self.assertEqual(errors, [])
        w1b = Wallet.objects.get(pk=w1_id).balance
        w2b = Wallet.objects.get(pk=w2_id).balance
        self.assertEqual(w1b + w2b, 1000)
        self.assertGreaterEqual(w1b, 0)
        successful_transfers = Transaction.objects.filter(
            wallet_id=w2_id, tx_type=Transaction.TRANSFER_IN
        ).count()
        self.assertEqual(w2b, successful_transfers * 100)

    def test_concurrent_same_idempotency_key_single_charge(self):
        tenant = make_tenant()
        client = APIClient()
        auth_key(client, tenant)
        _, w1 = create_user_wallet(client, "cc@example.com")
        w1_id = str(w1)
        api_key = tenant.api_key
        results = []

        def do_deposit(_: int):
            c = APIClient()
            c.credentials(HTTP_X_API_KEY=api_key)
            try:
                for attempt in range(10):
                    try:
                        r = c.post(
                            f"/api/wallets/{w1}/deposit/",
                            {"amount": 100, "idempotency_key": "race-key"},
                            format="json",
                        )
                        results.append(r.status_code)
                        return
                    except Exception as exc:  # noqa: BLE001 - sqlite lock contention retry
                        if "locked" in str(exc).lower() and attempt < 9:
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        results.append(f"error:{exc!r}")
                        return
            finally:
                for conn in connections.all():
                    conn.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(do_deposit, range(5)))

        self.assertTrue(all(s == 200 for s in results))
        self.assertEqual(Wallet.objects.get(pk=w1_id).balance, 100)
        self.assertEqual(Transaction.objects.filter(wallet_id=w1_id).count(), 1)
