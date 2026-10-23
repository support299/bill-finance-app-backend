from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


def issue_tokens(user) -> dict:
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def send_password_reset(email: str) -> None:
    """Silently does nothing for unknown emails so accounts cannot be enumerated."""
    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if not user:
        return
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = f"{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}"
    send_mail(
        "Reset your ImaxAccounting password",
        f"Use this link to choose a new password:\n\n{link}\n\nIf you did not request this, ignore this email.",
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )


def reset_password(uid: str, token: str, password: str):
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uid)))
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        return None
    if not default_token_generator.check_token(user, token):
        return None
    user.set_password(password)
    user.save(update_fields=["password"])
    return user
