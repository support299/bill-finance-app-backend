from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.core.services import audit

from . import serializers as s
from .services import issue_tokens, reset_password, send_password_reset


class AuthThrottleMixin:
    throttle_scope = "auth"


class RegisterView(AuthThrottleMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request):
        ser = s.RegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.save()
        audit(user, "register", "user", str(user.id))
        return Response({"user": s.UserSerializer(user).data, **issue_tokens(user)}, status=status.HTTP_201_CREATED)


class LoginView(AuthThrottleMixin, TokenObtainPairView):
    permission_classes = [AllowAny]
    authentication_classes: list = []


class RefreshView(AuthThrottleMixin, TokenRefreshView):
    permission_classes = [AllowAny]
    authentication_classes: list = []


class LogoutView(APIView):
    viewer_may_post = True  # signing out is always allowed

    def post(self, request):
        # Stateless JWT: the client discards its tokens. Endpoint exists for symmetry and auditing.
        audit(request.user, "logout", "user", str(request.user.id))
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetRequestView(AuthThrottleMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request):
        ser = s.PasswordResetRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        send_password_reset(ser.validated_data["email"])
        return Response({"detail": "If that email exists, a reset link has been sent."})


class PasswordResetConfirmView(AuthThrottleMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request):
        ser = s.PasswordResetConfirmSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        try:
            validate_password(d["password"])
        except DjangoValidationError as e:
            raise ValidationError({"password": list(e.messages)})
        user = reset_password(d["uid"], d["token"], d["password"])
        if not user:
            raise ValidationError({"token": ["This reset link is invalid or has expired."]})
        audit(user, "password_reset", "user", str(user.id))
        return Response({"detail": "Password updated."})


class ChangePasswordView(APIView):
    def post(self, request):
        ser = s.ChangePasswordSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        if not request.user.check_password(ser.validated_data["current_password"]):
            raise ValidationError({"current_password": ["Current password is incorrect."]})
        request.user.set_password(ser.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        return Response({"detail": "Password updated."})


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = s.UserSerializer

    def get_object(self):
        return self.request.user
