import hashlib
import hmac
import logging
import re
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.services import issue_tokens
from apps.core.models import AuditLog
from apps.core.services import audit
from apps.ledger.models import Ledger

from .models import GhlInvoice, GhlLocation
from .services import agency as agency_svc
from .services import invoices as inv_svc
from .services import linking, oauth, workspace
from .services import locations as loc_svc

logger = logging.getLogger(__name__)
User = get_user_model()
LOCATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
INVOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{6,40}$")


def _link_signature(location_id: str) -> str:
    return hmac.new(settings.GHL_LINK_SECRET.encode(), location_id.encode(), hashlib.sha256).hexdigest()


class InstallView(APIView):
    """Public onboarding entry point: redirects to the GHL consent screen.

    Everything (client id, redirect URI, scopes, version id) comes from the environment, so changing scopes
    never needs a code change. Optional ?state= is passed through so a caller can correlate the install.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request):
        missing = [
            name
            for name, value in (
                ("GHL_CLIENT_ID", settings.GHL_CLIENT_ID),
                ("GHL_REDIRECT_URI", settings.GHL_REDIRECT_URI),
                ("GHL_SCOPES", settings.GHL_SCOPES),
            )
            if not value
        ]
        if missing:
            logger.error("GHL install link is not configured; missing %s", ", ".join(missing))
            return Response(
                {"error": {"code": "not_configured", "message": "The app install link is not configured.", "details": None}},
                status=503,
            )
        params = {
            "response_type": "code",
            "redirect_uri": settings.GHL_REDIRECT_URI,
            "client_id": settings.GHL_CLIENT_ID,
            "scope": " ".join(settings.GHL_SCOPES),
            "version_id": settings.GHL_VERSION_ID,
        }
        if state := request.query_params.get("state", "")[:200]:
            params["state"] = state
        return HttpResponseRedirect(f"{settings.GHL_AUTH_URL}?{urlencode(params)}")  # spaces -> '+', '/' -> %2F


class OAuthCallbackView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request):
        code = request.query_params.get("code", "")
        if not 8 <= len(code) <= 512:
            return Response("Missing or invalid code", status=400)
        try:
            t = oauth.exchange_code(code)
        except Exception:  # noqa: BLE001
            logger.exception("OAuth install failed")
            return Response("Installation failed. Please try installing the app again.", status=502)
        # Record who installed: the subaccount (with its name) or, for an agency install, every installed subaccount.
        try:
            if t.get("locationId"):
                loc_svc.record_location(t["locationId"], company_id=t.get("companyId"))
            elif t.get("companyId"):
                loc_svc.sync_installed_locations()
        except Exception:  # noqa: BLE001 - the install itself succeeded; the hourly/admin sync can fill this in
            logger.exception("Could not record installed locations")
        from urllib.parse import quote

        # `link` lets a signed-in user attach this location to their own workspace (see services/linking.py).
        logger.info(
            "GHL install callback: userType=%s location=%s company=%s -> redirecting to the app with a link code",
            t.get("userType"),
            t.get("locationId"),
            t.get("companyId"),
        )
        link = quote(linking.make_link_code(t.get("locationId"), t.get("companyId")))
        dest = f"{settings.FRONTEND_URL}/"
        if t.get("locationId"):
            dest += f"?id={quote(t['locationId'])}&link={link}"
            if settings.GHL_LINK_SECRET:
                dest += f"&sig={_link_signature(t['locationId'])}"
        else:
            dest += f"?link={link}"
        return HttpResponseRedirect(dest)


class SessionView(APIView):
    """GHL subaccount sign-in from the bare link (?id=<location id>). Each location maps to its own hidden tenant user.

    Parity with the original app. If GHL_LINK_SECRET is set, a valid `sig` (HMAC-SHA256 of the id) is additionally required.
    Once GHL_SSO_SECRET is set this entry point is closed: the page must sign in through SsoSessionView, which knows who
    the person is.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "auth"

    def post(self, request):
        location_id = str(request.data.get("id", ""))
        deny = Response({"error": {"code": "forbidden", "message": "This link is not valid.", "details": None}}, status=403)
        if not LOCATION_ID_RE.match(location_id):
            return deny
        if settings.GHL_SSO_SECRET:
            return Response(
                {
                    "error": {
                        "code": "sso_required",
                        "message": "Open the accounting page from inside GoHighLevel to sign in.",
                        "details": None,
                    }
                },
                status=403,
            )
        if settings.GHL_LINK_SECRET and not hmac.compare_digest(str(request.data.get("sig", "")), _link_signature(location_id)):
            return deny
        try:
            loc, user = workspace.open_location(location_id)
        except workspace.WorkspaceDenied as exc:
            return Response({"error": {"code": exc.code, "message": str(exc), "details": None}}, status=403)
        audit(user, "ghl_link_sign_in", "ghl_location", location_id)
        return Response({**issue_tokens(user), "user_id": str(user.id)})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ScopedRateThrottle])
def ghl_webhook(request):
    body = request.data if isinstance(request.data, dict) else {}
    type_ = str(body.get("type", ""))
    print("type----------:", type_)
    if not type_ or len(type_) > 100:
        return Response("Bad request", status=400)
    location_id = body.get("locationId") or body.get("altId")
    # Unverified payloads never trigger destructive writes: an uninstall is only recorded
    # after the stored refresh token is confirmed to no longer work.
    if type_ == "UNINSTALL" and (location_id or body.get("companyId")):
        try:
            if location_id:
                oauth.confirm_uninstall(str(location_id))
            else:  # the agency removed the app from the whole company
                oauth.confirm_company_uninstall(str(body["companyId"]))
        except Exception:  # noqa: BLE001
            logger.exception("Uninstall check failed")
    # A new install: only the id is taken from the payload; the name and details are read back from GoHighLevel.
    if type_ == "INSTALL" and location_id and LOCATION_ID_RE.match(str(location_id)):
        try:
            loc_svc.record_location(str(location_id), company_id=body.get("companyId"), require_verified=True)
        except Exception:  # noqa: BLE001
            logger.exception("Install event could not be recorded")
    # Invoice events: the payload is never trusted; the invoice is re-read from GoHighLevel.
    invoice_id = body.get("_id") or body.get("invoiceId") or body.get("id")
    if re.match(r"^Invoice", type_, re.I) and location_id and isinstance(invoice_id, str) and INVOICE_ID_RE.match(invoice_id):
        user = inv_svc.user_for_location(str(location_id))
        if user:
            try:
                inv_svc.sync_invoice(str(location_id), user, invoice_id, "webhook", type_)
            except Exception:  # noqa: BLE001
                logger.exception("Invoice webhook sync failed")
                return Response("retry", status=500)  # let GoHighLevel retry; posting is idempotent
    return Response("ok")


ghl_webhook.cls.throttle_scope = "webhook"


class RefreshTokensView(APIView):
    """Scheduled job (cron secret, constant-time compare)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request):
        header = request.headers.get("authorization", "")
        m = re.match(r"^Bearer ([^\s,]+)$", header)
        if not settings.CRON_SECRET:
            return Response("Server configuration error", status=500)
        if not m or not hmac.compare_digest(
            hashlib.sha256(m.group(1).encode()).digest(), hashlib.sha256(settings.CRON_SECRET.encode()).digest()
        ):
            return Response("Unauthorized", status=401)
        return Response(oauth.refresh_expiring_tokens())


class SyncInvoicesView(APIView):
    """Imports / reconciles every GoHighLevel invoice for the signed-in workspace."""

    def post(self, request):
        locs = GhlLocation.objects.filter(user=request.user, active=True)
        if not locs.exists():
            raise ValidationError({"detail": "This workspace isn't linked to a GoHighLevel subaccount."})
        synced = failed = 0
        for loc in locs:
            r = inv_svc.sync_location_invoices(loc.location_id, request.user)
            synced += r["synced"]
            failed += r["failed"]
        return Response({"synced": synced, "failed": failed})


class LedgerInvoicesView(APIView):
    def get(self, request, ledger_id):
        if not Ledger.objects.for_user(request.user).filter(pk=ledger_id).exists():
            return Response({"invoices": []})
        rows = (
            GhlInvoice.objects.for_user(request.user)
            .filter(ledger_id=ledger_id)
            .order_by("-issue_date")
            .values(
                "id",
                "invoice_number",
                "issue_date",
                "due_date",
                "total",
                "tax",
                "amount_paid",
                "balance",
                "status",
                "needs_review",
                "review_reason",
            )
        )
        return Response({"invoices": list(rows)})


class LinksView(APIView):
    """GET: the GoHighLevel locations linked to the signed-in workspace."""

    def get(self, request):
        locs = GhlLocation.objects.filter(user=request.user).order_by("name", "location_id")
        return Response({"locations": [linking.summary(loc) for loc in locs]})


class LinkDetailView(APIView):
    def delete(self, request, location_id):
        loc = GhlLocation.objects.filter(location_id=location_id, user=request.user).first()
        if not loc:
            return Response({"error": {"code": "not_found", "message": "Not found.", "details": None}}, status=404)
        loc.user = None
        loc.save(update_fields=["user"])
        audit(request.user, "ghl_location_unlinked", "ghl_location", location_id)
        return Response(status=204)


class LinkOptionsView(APIView):
    """What a link code allows: the one location it names, or every installed location of its company to choose from."""

    def get(self, request):
        data = linking.read_link_code(request.query_params.get("code", ""))
        if data.get("loc"):
            locs = GhlLocation.objects.filter(location_id=data["loc"])
        elif data.get("co"):
            locs = GhlLocation.objects.filter(company_id=data["co"], active=True).order_by("name")
        else:
            locs = GhlLocation.objects.none()
        return Response({"choose": not data.get("loc"), "locations": [linking.summary(loc, request.user) for loc in locs]})


class LinkConfirmView(APIView):
    def post(self, request):
        data = linking.read_link_code(str(request.data.get("code", "")))
        location_id = data.get("loc") or str(request.data.get("location_id", ""))
        if not location_id:
            raise ValidationError({"location_id": "Choose a location."})
        loc = GhlLocation.objects.filter(location_id=location_id).first()
        # a company-level code may only link locations of that same company
        if not loc or (not data.get("loc") and loc.company_id != data.get("co")):
            raise ValidationError({"location_id": "That location is not part of this installation."})
        was_free = linking.link_state(loc, request.user) == "free"
        linking.link_location(loc, request.user)
        if was_free:
            from .tasks import import_location_invoices_task

            import_location_invoices_task.delay(loc.location_id, str(request.user.id))
        loc.refresh_from_db()
        return Response(linking.summary(loc, request.user))


# ------------------------------------------------------- agency support access
class AgencySessionView(APIView):
    """Agency sign-in from the link's company id + shared token; opens the client list."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "auth"

    def post(self, request):
        company = agency_svc.verify_access(str(request.data.get("company_id") or ""), str(request.data.get("token") or ""))
        agency = agency_svc.agency_user(company)
        agency.last_access_at = timezone.now()
        agency.save(update_fields=["last_access_at"])
        audit(agency.user, "agency_sign_in", "ghl_agency", company, {"via": "token"})
        return Response({**issue_tokens(agency.user), "company_id": company})


class AgencyClientsView(APIView):
    """The agency's onboarded sub-accounts: searchable, filterable and paginated across ALL of them (not just one page).

    Query params: q (name / email / location id), status = all | opened | not_opened, page, page_size (max 100).
    "opened" = has a workspace that can be viewed; "not_opened" = never opened the app. Clients linked to a personal
    workspace appear under "all" only.
    """

    MAX_PAGE_SIZE = 100

    def get(self, request):
        from django.core.paginator import EmptyPage, Paginator
        from django.db.models import Count, Q
        from django.db.models.functions import Lower

        from apps.banking.models import PlaidItem
        from apps.ledger.models import Transaction

        agency = agency_svc.agency_of(request.user)
        params = request.query_params
        opened_q = Q(user__isnull=False, user__email__endswith=linking.TENANT_DOMAIN)
        base = GhlLocation.objects.filter(company_id=agency.company_id, active=True)
        term = (params.get("q") or "").strip()
        if term:
            base = base.filter(Q(name__icontains=term) | Q(email__icontains=term) | Q(location_id__icontains=term))
        counts = {
            "all": base.count(),
            "opened": base.filter(opened_q).count(),
            "not_opened": base.filter(user__isnull=True).count(),
        }  # tab badges respect the search but not the status tab
        status = params.get("status", "all")
        qs = base.filter(opened_q) if status == "opened" else base.filter(user__isnull=True) if status == "not_opened" else base
        qs = qs.select_related("user").order_by(Lower("name").asc(nulls_last=True), "location_id")

        try:
            page_size = min(max(int(params.get("page_size", 25)), 1), self.MAX_PAGE_SIZE)
            page_no = max(int(params.get("page", 1)), 1)
        except ValueError:
            raise ValidationError({"page": "Page and page_size must be numbers."})
        paginator = Paginator(qs, page_size)
        try:
            page = paginator.page(page_no)
        except EmptyPage:
            page = paginator.page(paginator.num_pages)

        locs = list(page.object_list)
        user_ids = [loc.user_id for loc in locs if loc.user_id]  # activity is only counted for the clients on this page
        tx = dict(
            Transaction.objects.filter(user_id__in=user_ids, removed_at__isnull=True)
            .values_list("user_id")
            .annotate(n=Count("id"))
            .values_list("user_id", "n")
        )
        banks = dict(
            PlaidItem.objects.filter(user_id__in=user_ids).values_list("user_id").annotate(n=Count("id")).values_list("user_id", "n")
        )
        clients = []
        for loc in locs:
            uid = loc.user_id
            opened = bool(uid and linking.is_tenant(loc.user))
            clients.append(
                {
                    "location_id": loc.location_id,
                    "name": loc.name,
                    "email": loc.email,
                    "last_access_at": loc.last_access_at,
                    "can_open": opened,
                    "transaction_count": tx.get(uid, 0) if opened else 0,
                    "bank_count": banks.get(uid, 0) if opened else 0,
                    "note": None if opened else ("Linked to a personal workspace" if uid else "Has not opened the app yet"),
                }
            )
        recent = [
            {"at": a.created_at, "action": a.action, "location_id": a.entity_id, "who": a.metadata.get("name") or a.metadata.get("email")}
            for a in AuditLog.objects.filter(user=request.user, action__in=("agency_open_client", "agency_sign_in"))[:15]
        ]
        return Response(
            {
                "company_id": agency.company_id,
                "clients": clients,
                "count": paginator.count,
                "page": page.number,
                "page_size": page_size,
                "total_pages": paginator.num_pages,
                "counts": counts,
                "recent_activity": recent,
            }
        )


class AgencyOpenClientView(APIView):
    """Start a read-only session on one client's workspace (audited)."""

    def post(self, request, location_id):
        agency = agency_svc.agency_of(request.user)
        loc = GhlLocation.objects.filter(location_id=location_id, company_id=agency.company_id, active=True).select_related("user").first()
        if not loc:
            return Response({"error": {"code": "not_found", "message": "Client not found.", "details": None}}, status=404)
        if not loc.user_id or not linking.is_tenant(loc.user):
            raise ValidationError({"detail": "This client has no workspace to view yet."})
        audit(
            request.user,
            "agency_open_client",
            "ghl_location",
            loc.location_id,
            {"client": loc.name, "tenant_user": str(loc.user_id), "mode": "read_only"},
        )
        return Response(
            {**agency_svc.issue_support_tokens(loc.user, request.user, loc.location_id), "client_name": loc.name or loc.location_id}
        )
