"""
R21 — account provisioning (CLAUDE.md §2 "User provisioning").

1. Only EXEC or ADMIN creates an account, with name, email and role.
2. The system generates a random temporary password. Nothing here returns it
   to the caller, so the creator is never shown it.
3. It is emailed to the new user once. Only its hash is stored.
4. It expires 7 days after issue.
5-6. Until the user sets a new password, ForcePasswordChangeMiddleware lets
   them reach only the change-password page.

The email is sent inside the creating transaction: if sending fails, the
account is not created and the creator sees the error, rather than an account
existing whose password nobody received.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from core.audit import audit_actor

from .models import User

TEMPORARY_PASSWORD_LIFETIME = timedelta(days=7)


def generate_temporary_password():
    # ~96 bits of entropy, URL-safe so it survives being pasted from an email.
    return secrets.token_urlsafe(12)


def _require_provisioner(actor):
    if actor is None or not actor.is_authenticated or not actor.can_provision_accounts:
        raise PermissionDenied("Only the Managing Director or a System Admin can create accounts (R21).")


def _issue_temporary_password(user):
    """Set a fresh temporary password on ``user`` (unsaved) and return it."""
    password = generate_temporary_password()
    user.set_password(password)
    user.must_change_password = True
    user.temporary_password_expires_at = timezone.now() + TEMPORARY_PASSWORD_LIFETIME
    return password


def _email_temporary_password(user, password, *, reissued):
    subject = "Your IEA Operations account" + (" — new temporary password" if reissued else "")
    expires = timezone.localtime(user.temporary_password_expires_at)
    body = (
        f"Hello {user.first_name},\n\n"
        f"{'A new temporary password has been issued for' if reissued else 'An account has been created for you on'} "
        "the IEA Operations system.\n\n"
        f"Sign in with:\n  Email: {user.email}\n  Temporary password: {password}\n\n"
        f"This password expires on {expires:%d %B %Y at %H:%M} (East Africa Time). "
        "When you first sign in you will be asked to choose your own password.\n\n"
        "This password will not be shown or sent again. If it is lost or expires, "
        "ask the Managing Director or a System Admin to reissue one.\n"
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)


def provision_user(*, created_by, user):
    """
    Create ``user`` (an unsaved User with first_name, last_name, email, role)
    under R21. Returns the saved user; never returns the password.
    """
    _require_provisioner(created_by)
    if user.pk is not None:
        raise ValueError("provision_user() creates new accounts only; use reissue_temporary_password().")
    missing = [f for f in ("first_name", "last_name", "email", "role") if not getattr(user, f)]
    if missing:
        raise ValidationError({f: "This field is required (R21)." for f in missing})

    with transaction.atomic(), audit_actor(created_by):
        password = _issue_temporary_password(user)
        user.full_clean(exclude=["password"])
        user.save()
        _email_temporary_password(user, password, reissued=False)
    return user


def create_user_account(*, created_by, email, first_name, last_name, role):
    """Convenience wrapper around provision_user() for code outside the admin."""
    return provision_user(
        created_by=created_by,
        user=User(email=email, first_name=first_name, last_name=last_name, role=role),
    )


def reissue_temporary_password(*, issued_by, user):
    """Replace a lost or expired temporary password (§2 step 3-4)."""
    _require_provisioner(issued_by)
    with transaction.atomic(), audit_actor(issued_by):
        user = User.objects.select_for_update().get(pk=user.pk)
        password = _issue_temporary_password(user)
        user.save()
        _email_temporary_password(user, password, reissued=True)
    return user
