"""运行总览 API（只读聚合；单对象接口不分页，见 N-01）。"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .serializers import normalize

#: 允许的最大窗口/趋势天数（防止误传超大值导致无谓查询）
MAX_DAYS = 365


def _days_param(request, default):
    """解析 ``?days=``：非法值回退默认并夹到 ``[1, MAX_DAYS]``。"""
    raw = request.query_params.get('days') or request.query_params.get('window_days')
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_DAYS))


class OverviewView(APIView):
    """``GET /api/dashboard/overview/`` —— 全局 KPI 快照。

    一次请求返回执行 / 执行意向 / 委托单 / 资金 / 告警 / 配置 / 数据新鲜度七块，
    前端不再需要「拉多个列表自己算统计」。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        window_days = _days_param(request, services.DEFAULT_WINDOW_DAYS)
        return Response(normalize(services.overview(window_days=window_days)))


class ExecutionTrendView(APIView):
    """``GET /api/dashboard/execution-trend/?days=14`` —— 按自然日的执行趋势。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        days = _days_param(request, services.DEFAULT_TREND_DAYS)
        series = services.execution_trend(days=days)
        return Response(normalize({'days': days, 'series': series}))
