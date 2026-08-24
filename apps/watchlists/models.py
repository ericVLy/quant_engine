from django.db import models
from django.conf import settings

class Symbol(models.Model):
    MARKET_CHOICES = [
        ('A', 'A股'),
        ('HK', '港股'),
        ('US', '美股'),
    ]
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    exchange = models.CharField(max_length=20, blank=True)
    market = models.CharField(max_length=10, choices=MARKET_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.code} - {self.name}"

class Group(models.Model):
    name = models.CharField(max_length=50, unique=True)
    symbols = models.ManyToManyField(Symbol, related_name='groups', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

class Watchlist(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='watchlists')
    groups = models.ManyToManyField(Group, related_name='watchlists', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} 的自选池"
