"""Database-backed login limits, shared by every app worker."""
import hashlib
import hmac
import unicodedata
from datetime import timedelta
from ipaddress import ip_address

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import SecurityThrottle


def client_ip(request):
    remote = request.META.get('REMOTE_ADDR', '')
    # Production Gunicorn is loopback-only. Apache overwrites forwarding headers.
    if not settings.DEBUG and remote in ('127.0.0.1', '::1'):
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
        try:
            return str(ip_address(forwarded))
        except ValueError:
            pass
    return remote


def throttle_keys(request):
    username = unicodedata.normalize('NFKC', request.POST.get('username', '')[:150]).strip().casefold()
    values = [('account:' + username, settings.LOGIN_MAX_ATTEMPTS),
              ('address:' + client_ip(request), 50)]
    return [(hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest(), limit) for value, limit in values]


def is_throttled(request):
    now = timezone.now()
    return SecurityThrottle.objects.filter(key__in=[k for k, _ in throttle_keys(request)], blocked_until__gt=now).exists()


@transaction.atomic
def register_failure(request):
    now = timezone.now()
    window = timedelta(seconds=settings.LOGIN_WINDOW_SECONDS)
    for key, limit in sorted(throttle_keys(request)):
        SecurityThrottle.objects.get_or_create(key=key)
        throttle = SecurityThrottle.objects.select_for_update().get(key=key)
        if now - throttle.window_started >= window:
            throttle.failures = 0
            throttle.window_started = now
            throttle.blocked_until = None
        throttle.failures += 1
        if throttle.failures >= limit:
            throttle.blocked_until = now + window
        throttle.save(update_fields=['failures', 'window_started', 'blocked_until'])


def reset_account_limit(request):
    account_key = throttle_keys(request)[0][0]
    SecurityThrottle.objects.filter(key=account_key).delete()
