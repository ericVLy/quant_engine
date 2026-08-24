from django.db import models
from django.conf import settings
from apps.cases.models import Case

class Suite(models.Model):
    AGGREGATE_CHOICES = [
        ('weighted_sum', '加权求和'),
        ('vote', '投票'),
        ('and', '逻辑与'),
        ('or', '逻辑或'),
    ]
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('published', '已发布'),
        ('archived', '已归档'),
    ]
    name = models.CharField(max_length=100)
    aggregate_method = models.CharField(max_length=20, choices=AGGREGATE_CHOICES, default='weighted_sum')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return self.name

class Edge(models.Model):
    from_suite = models.ForeignKey(Suite, on_delete=models.CASCADE, related_name='out_edges')
    to_suite = models.ForeignKey(Suite, on_delete=models.CASCADE, related_name='in_edges')
    condition = models.JSONField(default=dict, blank=True)
    event_condition = models.JSONField(default=dict, blank=True)
    weight = models.FloatField(default=1.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['from_suite', 'to_suite', 'condition']]

    def __str__(self):
        return f"{self.from_suite} -> {self.to_suite}"
