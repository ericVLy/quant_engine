from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
	fieldsets = UserAdmin.fieldsets + (
		('扩展信息', {'fields': ('phone', 'company')}),
	)
	list_display = ('username', 'email', 'phone', 'company', 'is_staff', 'is_active')
