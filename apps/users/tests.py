from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase


User = get_user_model()


class UsersAPITest(APITestCase):
	def test_register_assigns_default_role(self):
		response = self.client.post('/api/users/register/', {
			'username': 'alice',
			'password': 'Strong-password-123',
			'password_confirm': 'Strong-password-123',
			'email': 'alice@example.com',
			'phone': '13800000000',
			'company': 'Quant Co',
		})

		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		user = User.objects.get(username='alice')
		self.assertTrue(user.check_password('Strong-password-123'))
		self.assertEqual(list(user.groups.values_list('name', flat=True)), ['user'])
		self.assertEqual(response.data['company'], 'Quant Co')

	def test_login_profile_update_and_logout(self):
		User.objects.create_user(username='alice', password='Strong-password-123')

		login_response = self.client.post('/api/users/login/', {
			'username': 'alice',
			'password': 'Strong-password-123',
		})
		self.assertEqual(login_response.status_code, status.HTTP_200_OK)

		profile_response = self.client.get('/api/users/profile/')
		self.assertEqual(profile_response.status_code, status.HTTP_200_OK)
		self.assertEqual(profile_response.data['username'], 'alice')

		update_response = self.client.patch('/api/users/profile/', {'company': 'New Co'})
		self.assertEqual(update_response.status_code, status.HTTP_200_OK)
		self.assertEqual(update_response.data['company'], 'New Co')

		logout_response = self.client.post('/api/users/logout/')
		self.assertEqual(logout_response.status_code, status.HTTP_204_NO_CONTENT)
		self.assertEqual(
			self.client.get('/api/users/profile/').status_code,
			status.HTTP_403_FORBIDDEN,
		)

	def test_profile_requires_authentication(self):
		response = self.client.get('/api/users/profile/')
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_only_admin_can_manage_roles(self):
		user = User.objects.create_user(username='alice', password='Strong-password-123')
		self.client.login(username='alice', password='Strong-password-123')
		response = self.client.post(f'/api/users/{user.id}/roles/', {'roles': []})
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

	def test_admin_can_manage_roles(self):
		admin = User.objects.create_user(
			username='admin', password='Strong-password-123', is_staff=True
		)
		target = User.objects.create_user(username='alice', password='Strong-password-123')
		Group.objects.create(name='analyst')
		self.client.force_authenticate(admin)

		response = self.client.post(
			f'/api/users/{target.id}/roles/', {'roles': ['analyst']}
		)

		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data['roles'], ['analyst'])
