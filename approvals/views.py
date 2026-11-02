"""
The MD's registration approval screen (§5 step 8, R15).

The decision is recorded only through Approval.objects.sign, which verifies the
signature and calls Tender.approve_registration in the same transaction (R19).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from accounts.models import Role
from tenders.models import Tender, TenderStatus as S

from .forms import RegistrationDecisionForm
from .models import Approval, ApprovalDecision, ApprovalKind


def _require_md(user):
    if user.role != Role.EXECUTIVE:
        raise PermissionDenied("Only the Managing Director decides registration approvals (R15).")


@login_required
def registration_queue(request):
    _require_md(request.user)
    tenders = (
        Tender.objects.filter(status=S.PENDING_REGISTRATION_APPROVAL)
        .select_related("client", "project_manager")
        .order_by("submission_deadline", "created_at")
    )
    return render(request, "approvals/registration_queue.html", {"tenders": tenders})


@login_required
@require_http_methods(["GET", "POST"])
def registration_decide(request, pk):
    _require_md(request.user)
    tender = get_object_or_404(Tender.objects.select_related("client", "project_manager", "category"), pk=pk)
    if tender.status != S.PENDING_REGISTRATION_APPROVAL:
        messages.info(request, f"{tender.reference} is not awaiting registration approval.")
        return redirect("tenders:detail", pk=tender.pk)

    form = RegistrationDecisionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            Approval.objects.sign(
                tender, ApprovalKind.REGISTRATION, d["decision"], request.user,
                typed_name=d["typed_name"], password=d["password"], comments=d["comments"],
            )
        except PermissionDenied as exc:  # signature did not verify
            form.add_error(None, str(exc))
        except ValidationError as exc:  # the tender refused the transition
            for message in exc.messages:
                form.add_error(None, message)
        else:
            if d["decision"] == ApprovalDecision.APPROVED:
                messages.success(request, f"{tender.reference} registered. The PM has been told work may begin.")
            else:
                messages.success(request, f"{tender.reference} declined and withdrawn. The PM has been told why.")
            return redirect("approvals:registration_queue")
    return render(request, "approvals/registration_decide.html", {"tender": tender, "form": form})
