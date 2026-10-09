from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers


User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    roles = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'first_name', 'last_name',
                  'phone', 'company', 'is_active', 'roles')
        read_only_fields = ('id', 'username', 'is_active', 'roles')

    def get_roles(self, obj):
        return list(obj.groups.values_list('name', flat=True))


class RegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ('username', 'password', 'password_confirm', 'email',
                  'first_name', 'last_name', 'phone', 'company')

    def validate(self, attrs):
        if attrs['password'] != attrs.pop('password_confirm'):
            raise serializers.ValidationError({'password_confirm': '两次密码不一致'})
        validate_password(attrs['password'])
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class LoginSerializer(serializers.Serializer):  # pylint: disable=abstract-method  # 仅用于登录入参校验，不经 create/update 落库
    username = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class RoleSerializer(serializers.Serializer):  # pylint: disable=abstract-method  # 仅用于角色入参校验，不经 create/update 落库
    roles = serializers.ListField(
        child=serializers.CharField(max_length=150), allow_empty=True
    )


class SetupSerializer(serializers.ModelSerializer):
    """部署初始化的入参校验（创建首个超级管理员）。

    与 :class:`RegistrationSerializer` 同构，但额外强制密码强度——该账号是系统最高权限，
    弱密码等于把整个平台交出去，故即便项目级 ``AUTH_PASSWORD_VALIDATORS`` 被调弱，
    这里也显式要求最小长度并拒绝纯数字/常见弱口令。
    """

    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ('username', 'password', 'password_confirm', 'email',
                  'first_name', 'last_name', 'phone', 'company')

    def validate_username(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('用户名不能为空')
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs.pop('password_confirm'):
            raise serializers.ValidationError({'password_confirm': '两次密码不一致'})
        validate_password(attrs['password'])
        password = attrs['password']
        if len(password) < 8:
            raise serializers.ValidationError(
                {'password': '管理员密码至少 8 位'}
            )
        if password.isdigit():
            raise serializers.ValidationError({'password': '密码不能为纯数字'})
        if attrs.get('email'):
            attrs['email'] = attrs['email'].strip()
        return attrs
