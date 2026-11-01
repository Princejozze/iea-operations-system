from django.contrib.auth.backends import ModelBackend

from .models import normalise_email


class IEAModelBackend(ModelBackend):
    """
    ModelBackend with two IEA rules:
    - the email typed at sign-in is matched case-insensitively;
    - an expired temporary password is dead (R21): the account cannot sign in
      until an EXEC or ADMIN reissues one.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is not None:
            username = normalise_email(username)
        return super().authenticate(request, username=username, password=password, **kwargs)

    def user_can_authenticate(self, user):
        if getattr(user, "temporary_password_expired", False):
            return False
        return super().user_can_authenticate(user)
