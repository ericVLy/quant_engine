from django.http import JsonResponse

def placeholder(request, message="占位接口"):
    return JsonResponse({'status': 'ok', 'message': message, 'app': 'watchlists'})
def list_create(request):
    return placeholder(request, "列表/创建")

def detail(request, pk):
    return placeholder(request, f"详情/更新/删除 #{pk}")
