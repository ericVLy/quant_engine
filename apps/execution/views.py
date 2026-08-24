from django.http import JsonResponse

def placeholder(request, message="占位接口"):
    return JsonResponse({'status': 'ok', 'message': message, 'app': 'execution'})
def trigger_plan(request):
    return JsonResponse({'status': 'ok', 'message': 'Plan triggered (simulated)'})

def suite_run_status(request, run_id):
    return JsonResponse({'status': 'ok', 'run_id': run_id, 'state': 'running'})
