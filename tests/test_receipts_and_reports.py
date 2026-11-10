import datetime
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.ghl.models import GhlLocation
from apps.ghl.services import media
from apps.ledger.models import Attachment, Category, Ledger, Transaction
from apps.ledger.services.gaap import gaap_flags
from tests.test_reconciliation import account, clear, start
from tests.test_reconciliation import tx as bank_tx

pytestmark = pytest.mark.django_db
D = datetime.date


def cat(name):
    return Category.objects.get(user__isnull=True, name=name)


def tx(user, amount, date, type_, category=None, **kw):
    kw.setdefault("merchant_name", "m")
    return Transaction.objects.create(
        user=user, amount=amount, transaction_date=date, user_category=category, **gaap_flags(type_), **kw
    )


# ------------------------------------------------------------------ attachments
@pytest.fixture
def fake_media(monkeypatch):
    calls = {"uploaded": [], "deleted": []}

    def upload(loc, name, content, ctype):
        calls["uploaded"].append((loc.location_id, name, len(content)))
        return {"file_id": "f1", "url": "https://cdn.example/f1.pdf"}

    monkeypatch.setattr(media, "upload", upload)
    monkeypatch.setattr(media, "delete", lambda loc, fid: calls["deleted"].append((loc, fid)))
    monkeypatch.setattr(media, "fetch", lambda url: SimpleNamespace(content=b"%PDF", headers={}))
    return calls


def _pdf(name="r.pdf", size=4):
    return SimpleUploadedFile(name, b"%PDF"[:size].ljust(size, b"0"), content_type="application/pdf")


def test_attachment_upload_download_delete(api_a, user_a, fake_media):
    GhlLocation.objects.create(location_id="loc1", user=user_a)
    t = tx(user_a, -20, D(2026, 1, 1), "operating_expense")
    r = api_a.post(f"/api/v1/transactions/{t.id}/attachments/", {"file": _pdf()}, format="multipart")
    assert r.status_code == 201, r.data
    assert fake_media["uploaded"] == [("loc1", "r.pdf", 4)]
    att = Attachment.objects.get()
    assert (att.ghl_location_id, att.ghl_file_id) == ("loc1", "f1")
    assert api_a.get(f"/api/v1/transactions/{t.id}/attachments/").data[0]["name"] == "r.pdf"
    assert api_a.get("/api/v1/transactions/").data["results"][0]["attachment_count"] == 1
    assert api_a.get(f"/api/v1/attachments/{att.id}/").content == b"%PDF"
    assert api_a.delete(f"/api/v1/attachments/{att.id}/").status_code == 204
    assert fake_media["deleted"] == [("loc1", "f1")]


def test_attachment_rules_and_isolation(api_a, api_b, user_a, user_b, fake_media):
    GhlLocation.objects.create(location_id="loc1", user=user_a)
    t = tx(user_a, -20, D(2026, 1, 1), "operating_expense")
    url = f"/api/v1/transactions/{t.id}/attachments/"
    assert api_a.post(url, {"file": SimpleUploadedFile("x.exe", b"x")}, format="multipart").status_code == 400
    big = SimpleUploadedFile("big.pdf", b"0" * (10 * 1024 * 1024 + 1), content_type="application/pdf")
    assert api_a.post(url, {"file": big}, format="multipart").status_code == 400
    assert api_a.post(url, {}, format="multipart").status_code == 400
    assert api_b.post(url, {"file": _pdf()}, format="multipart").status_code == 404  # other tenant
    ok = api_a.post(url, {"file": _pdf()}, format="multipart")
    assert api_b.get(f"/api/v1/attachments/{ok.data['id']}/").status_code == 404
    assert api_b.delete(f"/api/v1/attachments/{ok.data['id']}/").status_code == 404


def test_attachment_needs_ghl_location(api_a, user_a, fake_media):
    t = tx(user_a, -20, D(2026, 1, 1), "operating_expense")
    r = api_a.post(f"/api/v1/transactions/{t.id}/attachments/", {"file": _pdf()}, format="multipart")
    assert r.status_code == 400 and "GoHighLevel" in str(r.data)


def test_media_folder_created_once_and_cached(user_a, monkeypatch):
    loc = GhlLocation.objects.create(location_id="loc1", user=user_a)
    posts = []

    class Res:
        def __init__(self, body, status=200):
            self.body, self.status_code, self.content = body, status, b"{}"

        def json(self):
            return self.body

    monkeypatch.setattr(media, "get_location_access_token", lambda _l: "tok")
    monkeypatch.setattr(media.httpx, "get", lambda *a, **k: Res({"files": []}))

    def post(url, **kw):
        posts.append(url)
        if url.endswith("/medias/folder"):
            return Res({"_id": "folder1"})
        assert kw["data"]["parentId"] == "folder1"
        return Res({"fileId": "f9", "url": "https://cdn/x"})

    monkeypatch.setattr(media.httpx, "post", post)
    assert media.upload(loc, "a.pdf", b"x", "application/pdf") == {"file_id": "f9", "url": "https://cdn/x"}
    media.upload(loc, "b.pdf", b"x", "application/pdf")
    assert sum(u.endswith("/medias/folder") for u in posts) == 1  # created once, then cached on the location
    loc.refresh_from_db()
    assert loc.media_folder_id == "folder1"


# ---------------------------------------------------------------- manual entries
def test_manual_entry_without_ledger(api_a, user_a):
    r = api_a.post(
        "/api/v1/entries/",
        {"date": "2026-01-05", "description": "Cash lunch", "amount": "12.50", "type": "operating_expense"},
        format="json",
    )
    assert r.status_code == 201, r.data
    t = Transaction.objects.get(pk=r.data["id"])
    assert t.ledger_id is None and t.amount == -12.5 and t.source == "manual"


# ------------------------------------------------------------------- sub-categories
def test_subcategory_rules(api_a, api_b):
    parent = cat("Salaries & Wages")
    ok = api_a.post("/api/v1/categories/", {"name": "Alice", "type": "operating_expense", "parent_category": str(parent.id)}, format="json")
    assert ok.status_code == 201, ok.data
    bad_type = api_a.post("/api/v1/categories/", {"name": "X", "type": "revenue", "parent_category": str(parent.id)}, format="json")
    assert bad_type.status_code == 400
    deep = api_a.post("/api/v1/categories/", {"name": "Deep", "type": "operating_expense", "parent_category": ok.data["id"]}, format="json")
    assert deep.status_code == 400
    # a parent with children cannot become a child
    mine = api_a.post("/api/v1/categories/", {"name": "Mine", "type": "operating_expense"}, format="json").data["id"]
    api_a.post("/api/v1/categories/", {"name": "Kid", "type": "operating_expense", "parent_category": mine}, format="json")
    assert api_a.patch(f"/api/v1/categories/{mine}/", {"parent_category": str(parent.id)}, format="json").status_code == 400
    # another tenant's category cannot be a parent
    assert api_b.post("/api/v1/categories/", {"name": "Z", "type": "operating_expense", "parent_category": mine}, format="json").status_code == 400


def test_pl_rollup_and_sort(api_a, user_a):
    sal = cat("Salaries & Wages")
    alice = Category.objects.create(user=user_a, name="Alice", type="operating_expense", parent_category=sal)
    bob = Category.objects.create(user=user_a, name="Bob", type="operating_expense", parent_category=sal)
    rent = cat("Rent or Lease")
    tx(user_a, -300, D(2026, 1, 2), "operating_expense", alice)
    tx(user_a, -100, D(2026, 1, 3), "operating_expense", bob)
    tx(user_a, -50, D(2026, 1, 4), "operating_expense", sal)
    tx(user_a, -900, D(2026, 1, 5), "operating_expense", rent)
    tx(user_a, -10, D(2026, 1, 6), "operating_expense", None)
    q = "/api/v1/reports/accounting/?from=2026-01-01&to=2026-01-31"
    lines = api_a.get(q).data["profit_and_loss"]["operating_expenses"]
    assert [ln["name"] for ln in lines] == ["Rent or Lease", "Salaries & Wages", "Uncategorized"]  # chart order (Rent=130 < Salaries=160)
    sal_line = lines[1]
    assert sal_line["value"] == 450 and sal_line["category_id"] == str(sal.id)
    assert {c["name"]: c["value"] for c in sal_line["children"]} == {"Alice": 300, "Bob": 100, "Salaries & Wages (general)": 50}
    assert str(alice.id) in sal_line["category_ids"] and str(sal.id) in sal_line["category_ids"]
    assert api_a.get(q + "&pl_sort=amount").data["profit_and_loss"]["operating_expenses"][0]["name"] == "Rent or Lease"
    by_amount = api_a.get(q + "&pl_sort=name").data["profit_and_loss"]["operating_expenses"]
    assert [ln["name"] for ln in by_amount] == ["Rent or Lease", "Salaries & Wages", "Uncategorized"]
    assert api_a.get(q).data["profit_and_loss"]["total_operating_expenses"] == 1360  # rollup never changes totals
    qs = "&".join(f"category_ids={i}" for i in sal_line["category_ids"])
    got = api_a.get(f"/api/v1/transactions/?{qs}&reportable=true").data
    assert got["count"] == 3  # parent + both children


def test_pl_sort_by_amount_is_largest_first(api_a, user_a):
    tx(user_a, -900, D(2026, 1, 5), "operating_expense", cat("Utilities"))
    tx(user_a, -100, D(2026, 1, 5), "operating_expense", cat("Insurance"))
    q = "/api/v1/reports/accounting/?from=2026-01-01&to=2026-01-31"
    names = lambda mode: [ln["name"] for ln in api_a.get(f"{q}&pl_sort={mode}").data["profit_and_loss"]["operating_expenses"]]  # noqa: E731
    assert names("amount") == ["Utilities", "Insurance"]
    assert names("name") == ["Insurance", "Utilities"]
    assert names("chart") == ["Insurance", "Utilities"]  # Insurance (90) before Utilities (200)


# ------------------------------------------------------- balance sheet / TB / tax
def test_balance_sheet_drill_down_and_trial_balance_check(api_a, user_a):
    led = Ledger.objects.create(user=user_a, label="Customer", account_class="current_asset")
    tx(user_a, 700, D(2026, 1, 13), "asset", None, ledger=led, source="manual", accounting_status="accounts_receivable")
    tx(user_a, 100, D(2026, 1, 14), "asset", None, ledger=led, source="manual", accounting_status="received")
    r = api_a.get("/api/v1/reports/accounting/?from=2026-01-01&to=2026-03-31").data
    assert r["balance_sheet"]["current_assets"][0]["kind"] == "ledger"
    assert r["balance_sheet"]["equity"][-1]["kind"] == "retained"
    assert r["trial_balance"]["balanced"] is False and r["trial_balance"]["difference"] == 700
    got = api_a.get(f"/api/v1/transactions/?ledger_ids={led.id}&exclude_settled=true&to=2026-03-31&reportable=true").data
    assert got["count"] == 1  # the balance-sheet line counts only the open item


def test_sales_tax_report(api_a, user_a):
    tax = cat("Sales Tax Payable")
    tx(user_a, 80, D(2026, 1, 10), "liability", tax, source="manual")
    tx(user_a, 20, D(2026, 2, 10), "liability", None, source="ghl", external_ref="ghl:inv:1:tax")
    tx(user_a, -50, D(2026, 2, 20), "liability", tax, source="manual")
    tx(user_a, 999, D(2026, 2, 20), "revenue", cat("Sales Revenue"), source="manual")
    st = api_a.get("/api/v1/reports/accounting/?from=2026-02-01&to=2026-03-31").data["sales_tax"]
    assert (st["collected"], st["remitted"], st["balance_due"]) == (20, 50, 50)  # period rows; balance is cumulative
    assert len(st["rows"]) == 2


# --------------------------------------------------------------------- unreconciled
def test_unreconciled_list(api_a, api_b, user_a):
    acct = account(user_a)
    a = bank_tx(user_a, acct, -10, 5)
    b = bank_tx(user_a, acct, 30, 6)
    c = bank_tx(user_a, acct, -5, 7, pending=True)
    rid = start(api_a, acct, 1020, beginning=1000)
    clear(api_a, rid, [a, b])
    url = f"/api/v1/reconciliations/unreconciled/?account_id={acct.id}"
    r = api_a.get(url).data
    assert r["count"] == 2 and {t["in_progress"] for t in r["transactions"]} == {True}  # pending c is excluded
    api_a.post(f"/api/v1/reconciliations/{rid}/finish/")
    r = api_a.get(url).data
    assert r["count"] == 0 and r["reconciled_through"] is not None
    assert c.pending
    assert api_b.get(url).status_code == 404


def test_manual_sales_tax_payment(api_a, user_a):
    tax = cat("Sales Tax Payable")
    body = {"date": "2026-02-01", "description": "State payment", "amount": "50", "type": "liability", "category_id": str(tax.id)}
    paid = api_a.post("/api/v1/entries/", {**body, "direction": "out"}, format="json")
    got = api_a.post("/api/v1/entries/", {**body, "description": "Collected", "amount": "80"}, format="json")
    assert paid.status_code == got.status_code == 201
    assert Transaction.objects.get(pk=paid.data["id"]).amount == -50
    assert Transaction.objects.get(pk=got.data["id"]).amount == 80  # default direction is "in"
    # editing without a direction keeps a payment a payment
    api_a.patch(f"/api/v1/entries/{paid.data['id']}/", {**body, "description": "Renamed"}, format="json")
    assert Transaction.objects.get(pk=paid.data["id"]).amount == -50
    st = api_a.get("/api/v1/reports/accounting/?from=2026-02-01&to=2026-02-28").data["sales_tax"]
    assert (st["collected"], st["remitted"], st["balance_due"]) == (80, 50, 30)


def test_rule_preview(api_a, api_b, user_a):
    meals = cat("Meals & Entertainment")
    tx(user_a, -9, D(2026, 1, 1), "operating_expense", None, merchant_name="Starbucks #1")
    tx(user_a, -8, D(2026, 1, 2), "operating_expense", meals, merchant_name="STARBUCKS #2")  # already in the target category
    tx(user_a, -7, D(2026, 1, 3), "operating_expense", None, merchant_name="Shell")
    body = {"keyword": "starbucks", "match_type": "contains", "category_id": str(meals.id)}
    r = api_a.post("/api/v1/rules/preview/", body, format="json").data
    assert (r["previewable"], r["matching"], r["would_change"]) == (True, 2, 1)
    assert r["sample"][0]["description"] == "Starbucks #1" and r["sample"][0]["current_category"] is None
    assert api_a.post("/api/v1/rules/preview/", {**body, "match_type": "regex"}, format="json").data["previewable"] is False
    assert api_a.post("/api/v1/rules/preview/", {"keyword": "x"}, format="json").status_code == 400
    assert api_b.post("/api/v1/rules/preview/", body, format="json").data["matching"] == 0  # other tenant's data is invisible
    assert Transaction.objects.filter(user_category=meals).count() == 1  # preview changed nothing


def test_category_reorder_changes_my_pl_only(api_a, api_b, user_a, user_b):
    util, ins = cat("Utilities"), cat("Insurance")  # default order: Insurance (90) then Utilities (200)
    for u in (user_a, user_b):
        tx(u, -10, D(2026, 1, 5), "operating_expense", util)
        tx(u, -10, D(2026, 1, 5), "operating_expense", ins)
    q = "/api/v1/reports/accounting/?from=2026-01-01&to=2026-01-31"
    names = lambda c: [ln["name"] for ln in c.get(q).data["profit_and_loss"]["operating_expenses"]]  # noqa: E731
    assert names(api_a) == ["Insurance", "Utilities"]
    r = api_a.post("/api/v1/categories/reorder/", {"ids": [str(util.id), str(ins.id)]}, format="json")
    assert r.status_code == 200, r.data
    assert names(api_a) == ["Utilities", "Insurance"]
    assert names(api_b) == ["Insurance", "Utilities"]  # another tenant keeps the default
    listed = {c["name"]: c["sort_order"] for c in api_a.get("/api/v1/categories/").data}
    assert listed["Utilities"] < listed["Insurance"]
    assert {c["name"]: c["sort_order"] for c in api_b.get("/api/v1/categories/").data}["Utilities"] == 200


def test_category_reorder_validation(api_a):
    util, rev = cat("Utilities"), cat("Sales Revenue")
    post = lambda ids: api_a.post("/api/v1/categories/reorder/", {"ids": ids}, format="json").status_code  # noqa: E731
    assert post([]) == 400
    assert post([str(util.id), str(util.id)]) == 400  # duplicate
    assert post([str(util.id), str(rev.id)]) == 400  # different types
    assert post([str(util.id), "00000000-0000-0000-0000-000000000000"]) == 400
    assert post(["not-a-uuid"]) == 400
