import time

from django.contrib.auth import logout
from django.shortcuts import redirect


class AccountSessionMiddleware:
    """Expire inactivity without allowing background status polls to keep sessions alive."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            now = time.time()
            last = request.session.get('last_activity', now)
            if now - last > 1800:
                logout(request)
                return redirect('login')
            if not request.path.endswith('/status/'):
                request.session['last_activity'] = now
        return self.get_response(request)


class PrivacyHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['Cache-Control'] = 'no-store, private, max-age=0'
        response['Pragma'] = 'no-cache'
        response['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        response['Referrer-Policy'] = 'same-origin'
        response['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        return response
