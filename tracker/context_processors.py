from django.utils import timezone
from django.conf import settings

def workspace(request):
    user = getattr(request, 'user', None)
    context = {'is_manager': bool(user and user.is_authenticated and user.is_staff),
            'server_today': timezone.localdate(), 'company_timezone': timezone.get_current_timezone_name(),
            'demo_mode': settings.DEMO_MODE}
    return context
