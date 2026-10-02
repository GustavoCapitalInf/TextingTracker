from django.utils import timezone
from django.conf import settings

from .appearance import COLOR_SCHEMES, current_theme

def workspace(request):
    user = getattr(request, 'user', None)
    initials = ''
    if user and user.is_authenticated:
        names = [user.first_name, user.last_name] if user.first_name else [user.get_username()]
        initials = ''.join(next((c for c in name if c.isalpha()), '') for name in names).upper() or '?'
    context = {'is_manager': bool(user and user.is_authenticated and user.is_staff), 'user_initials': initials,
            'server_today': timezone.localdate(), 'company_timezone': timezone.get_current_timezone_name(),
            'demo_mode': settings.DEMO_MODE}
    context['theme'] = current_theme(request)
    context['color_scheme'] = COLOR_SCHEMES[context['theme']]
    return context
