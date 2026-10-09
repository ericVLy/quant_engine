"""用户认证视图。

部署初始化端点（``setup/``）说明见 :mod:`apps.users.setup`——该端点在初始化前无需认证，
因此权限判定只依赖「是否仍需引导」，完成后永久关闭。
"""
import logging

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.models import Group
from django.db import OperationalError
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import (LoginSerializer, RegistrationSerializer, RoleSerializer,
                          SetupSerializer, UserSerializer)
from .setup import SetupAlreadyCompleted, complete_setup, is_setup_required

logger = logging.getLogger(__name__)


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


class SetupStatusView(APIView):
    """查询是否仍需要初始化引导（无需认证）。

    前端据此决定是否强制跳转引导页；已完成后恒为 ``false``，故引导不会被再次启用。
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({'setup_required': is_setup_required()})


class SetupView(APIView):
    """创建首个超级管理员并永久关闭引导（无需认证，仅在未初始化时可用）。"""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        # 先判状态：已完成直接 403，不进入密码校验（避免无谓开销与细节暴露）
        if not is_setup_required():
            return Response(
                {'detail': '系统已完成初始化，引导已关闭'},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = SetupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            created = complete_setup(
                username=data['username'],
                password=data['password'],
                email=data.get('email', ''),
                phone=data.get('phone', ''),
                company=data.get('company', ''),
                first_name=data.get('first_name', ''),
                last_name=data.get('last_name', ''),
            )
        except SetupAlreadyCompleted:
            return Response(
                {'detail': '系统已完成初始化，引导已关闭'},
                status=status.HTTP_403_FORBIDDEN,
            )
        except OperationalError:
            # 并发首次初始化时的写锁竞争（SQLite）：另一请求多半已完成初始化。
            # 归一为 403 而非 500，避免把可预期的竞争暴露成服务端错误。
            logger.warning('部署初始化遇到写锁竞争，按已完成处理', exc_info=True)
            return Response(
                {'detail': '系统已完成初始化，引导已关闭'},
                status=status.HTTP_403_FORBIDDEN,
            )
        # 创建即登录，省去用户再手工输一次密码
        user = authenticate(request, username=created.username, password=data['password'])
        if user is not None:
            login(request, user)
            return Response(user_response(user), status=status.HTTP_201_CREATED)
        return Response(user_response(created), status=status.HTTP_201_CREATED)
