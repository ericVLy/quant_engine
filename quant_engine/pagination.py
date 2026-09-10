"""统一分页组件。

对应非功能需求 N-01「所有 API 支持分页」：所有列表接口统一输出分页结构。

分页参数约定（兼容旧客户端）：
- ``page``      ：页码，从 1 开始（默认 1）
- ``page_size`` ：每页条数（默认 20，最大 500）
- ``limit``     ：旧客户端兼容别名，等价于 ``page_size``（Datasources/Plans 等旧调用使用 ``{limit: 500}``）

统一响应结构：

.. code-block:: json

    {
        "count": 123,
        "next": "http://host/api/xxx/?page=2&page_size=20",
        "previous": null,
        "page": 1,
        "total_pages": 7,
        "results": [ ... ]
    }
"""
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardResultsPagination(PageNumberPagination):
    """项目统一分页类。

    - ``results`` 保持列表数据，``count / next / previous`` 与 DRF 标准分页
      PageNumberPagination 契约一致；
    - 额外提供 ``page`` 与 ``total_pages``，便于前端直接渲染分页器；
    - 兼容旧客户端传入 ``limit`` 作为 ``page_size`` 的别名（此前前端使用
      ``{limit: 500}`` 拉取全量下拉选项）。
    """

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 500

    # 旧客户端兼容参数：limit 作为 page_size 的别名
    limit_query_param = 'limit'

    def get_page_size(self, request):
        limit = request.query_params.get(self.limit_query_param)
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = None
            if limit and 0 < limit <= self.max_page_size:
                return limit
        return super().get_page_size(request)

    def get_paginated_response(self, data):
        return Response({
            'count': self.page.paginator.count,
            'next': self.get_next_link(),
            'previous': self.get_previous_link(),
            'page': self.page.number,
            'total_pages': self.page.paginator.num_pages,
            'results': data,
        })