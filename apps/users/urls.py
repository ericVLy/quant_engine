from django.urls import path
from .views import LoginView, LogoutView, ProfileView, RegisterView, UserRoleView

app_name = 'users'
urlpatterns = [
	path('register/', RegisterView.as_view(), name='register'),
	path('login/', LoginView.as_view(), name='login'),
	path('logout/', LogoutView.as_view(), name='logout'),
	path('profile/', ProfileView.as_view(), name='profile'),
	path('<int:user_id>/roles/', UserRoleView.as_view(), name='roles'),
]
