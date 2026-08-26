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


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class RoleSerializer(serializers.Serializer):
    roles = serializers.ListField(
        child=serializers.CharField(max_length=150), allow_empty=True
    )