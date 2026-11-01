from django.shortcuts import redirect
from django.urls import reverse


class ForcePasswordChangeMiddleware:
    """
    R21: while ``must_change_password`` is true the user can reach nothing but
    the change-password form (and log out) — not the dashboard, not the admin,
    not any other URL, whatever the HTTP method.

    An expired temporary password needs no handling here: the auth backend
    refuses the account (IEAModelBackend.user_can_authenticate), and Django's
    get_user() applies that on every request, so an open session becomes
    anonymous the moment the temporary password expires.

    Place after AuthenticationMiddleware and MessageMiddleware. Static files
    never reach it (WhiteNoise answers them earlier in the stack).
    """

    ALLOWED_URL_NAMES = ("password_change", "logout")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.must_change_password:
            allowed = {reverse(name) for name in self.ALLOWED_URL_NAMES}
            if request.path_info not in allowed:
                return redirect("password_change")
        return self.get_response(request)
