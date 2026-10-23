from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification
from .serializers import MarkReadSerializer, NotificationSerializer


class NotificationListView(generics.ListAPIView):
    serializer_class = NotificationSerializer
    pagination_class = None

    def get_queryset(self):
        return Notification.objects.for_user(self.request.user)[:30]


class NotificationMarkReadView(APIView):
    def post(self, request):
        ser = MarkReadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        qs = Notification.objects.for_user(request.user).filter(read=False)
        if ser.validated_data.get("ids"):
            qs = qs.filter(id__in=ser.validated_data["ids"])
        qs.update(read=True)
        return Response(status=status.HTTP_204_NO_CONTENT)
