"""本 app 只做只读聚合，**不定义模型**（因此没有 migrations 目录）。

聚合口径全部在 ``services.py``；响应字段白名单由 ``serializers.normalize`` 保证。
"""
