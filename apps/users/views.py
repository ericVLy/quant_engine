from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import LoginSerializer, RegistrationSerializer, RoleSerializer, UserSerializer


User = get_user_model()
DEFAULT_ROLE = 'user'


def user_response(user):
    return UserSerializer(user).data


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        group, _ = Group.objects.get_or_create(name=DEFAULT_ROLE)
        user.groups.add(group)
        return Response(user_response(user), status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(request, **serializer.validated_data)
        if user is None:
            return Response({'detail': '用户名或密码错误'}, status=status.HTTP_400_BAD_REQUEST)
        login(request, user)
        return Response(user_response(user))


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(user_response(request.user))

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    put = patch


class UserRoleView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, user_id):
        if not request.user.is_staff and not request.user.groups.filter(name='admin').exists():
            return Response({'detail': '仅管理员可管理用户角色'}, status=status.HTTP_403_FORBIDDEN)
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'detail': '用户不存在'}, status=status.HTTP_404_NOT_FOUND)
        serializer = RoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        roles = serializer.validated_data['roles']
        groups = list(Group.objects.filter(name__in=roles))
        missing = sorted(set(roles) - {group.name for group in groups})
        if missing:
            return Response({'roles': [f'角色不存在: {name}' for name in missing]},
                            status=status.HTTP_400_BAD_REQUEST)
        user.groups.set(groups)
        return Response(user_response(user))
