"""Hosting on Vercel: settings derived from Vercel's environment, and the visitor address behind its proxy."""
import json
import os
import subprocess
import sys

from django.conf import settings
from django.test import RequestFactory, SimpleTestCase, override_settings

from tracker.authentication import client_ip

SECRET = 'x' * 64
PROBE = ('import json, django; django.setup(); from django.conf import settings as s; '
         'd = s.DATABASES["default"]; print(json.dumps({"debug": s.DEBUG, "demo": s.DEMO_MODE, "hosts": s.ALLOWED_HOSTS, '
         '"origins": s.CSRF_TRUSTED_ORIGINS, "options": d.get("OPTIONS", {}), "no_cursors": d.get("DISABLE_SERVER_SIDE_CURSORS"), '
         '"upload": s.IMPORT_MAX_BYTES, "trust_ip": s.TRUST_VERCEL_IP_HEADERS, "ssl_redirect": s.SECURE_SSL_REDIRECT}))')


def load_settings(**env):
    """Import the settings in a fresh interpreter with exactly these overrides (no local .env values leak in)."""
    base = {key: value for key, value in os.environ.items() if not key.startswith(('DJANGO_', 'VERCEL', 'DATABASE_', 'PHONETRACKER_'))}
    blank = {name: '' for name in ('DJANGO_ENV', 'DJANGO_SECRET_KEY', 'DJANGO_ALLOWED_HOSTS', 'DJANGO_CSRF_TRUSTED_ORIGINS',
                                   'DATABASE_URL', 'PHONETRACKER_DEMO', 'PHONETRACKER_DATA_DIR')}
    result = subprocess.run([sys.executable, '-c', PROBE], cwd=settings.BASE_DIR, capture_output=True, text=True,
                            env={**base, **blank, 'DJANGO_SETTINGS_MODULE': 'config.settings', **env})
    return json.loads(result.stdout) if result.returncode == 0 else result.stderr


VERCEL = {'VERCEL': '1', 'VERCEL_ENV': 'production', 'DJANGO_SECRET_KEY': SECRET,
          'VERCEL_PROJECT_PRODUCTION_URL': 'phonetracker.vercel.app', 'VERCEL_URL': 'phonetracker-a1b2c3.vercel.app',
          'DATABASE_URL': 'postgresql://app:pw@ep-calm-sun-123456-pooler.us-east-1.aws.neon.tech/neondb?sslmode=require'}


class VercelSettingsTests(SimpleTestCase):
    def test_vercel_production_is_locked_down_and_knows_its_own_domains(self):
        loaded = load_settings(**VERCEL, DJANGO_ENV='development', PHONETRACKER_DEMO='1')
        self.assertIsInstance(loaded, dict, loaded)
        self.assertEqual((loaded['debug'], loaded['demo'], loaded['ssl_redirect'], loaded['trust_ip']), (False, False, True, True))
        self.assertEqual(loaded['hosts'], ['phonetracker.vercel.app', 'phonetracker-a1b2c3.vercel.app'])
        self.assertEqual(loaded['origins'], ['https://phonetracker.vercel.app', 'https://phonetracker-a1b2c3.vercel.app'])
        self.assertEqual(loaded['upload'], 4 * 1024 * 1024)

    def test_neon_pooled_url_turns_off_prepared_statements_and_server_cursors(self):
        pooled = load_settings(**VERCEL)
        self.assertEqual((pooled['options'], pooled['no_cursors']), ({'sslmode': 'require', 'prepare_threshold': None}, True))
        direct = load_settings(**{**VERCEL, 'DATABASE_URL': VERCEL['DATABASE_URL'].replace('-pooler', '')})
        self.assertEqual((direct['options'], direct['no_cursors']), ({'sslmode': 'require'}, False))

    def test_a_custom_domain_is_added_alongside_vercels(self):
        loaded = load_settings(**VERCEL, DJANGO_ALLOWED_HOSTS='leads.capital-infusion.com',
                               DJANGO_CSRF_TRUSTED_ORIGINS='https://leads.capital-infusion.com')
        self.assertEqual(loaded['hosts'][0], 'leads.capital-infusion.com')
        self.assertIn('https://leads.capital-infusion.com', loaded['origins'])

    def test_vercel_production_still_needs_its_secrets(self):
        self.assertIn('DJANGO_SECRET_KEY', load_settings(**{**VERCEL, 'DJANGO_SECRET_KEY': ''}))
        self.assertIn('DATABASE_URL', load_settings(**{**VERCEL, 'DATABASE_URL': ''}))

    def test_off_vercel_nothing_changes(self):
        local = load_settings(DJANGO_ENV='development')
        self.assertEqual((local['debug'], local['trust_ip'], local['upload']), (True, False, 5 * 1024 * 1024))
        self.assertIn('DJANGO_ALLOWED_HOSTS', load_settings(DJANGO_ENV='production', DJANGO_SECRET_KEY=SECRET,
                                                            DATABASE_URL='postgresql://u:p@db.internal/app'))


class VercelClientAddressTests(SimpleTestCase):
    def request(self, **headers):
        return RequestFactory().post('/login/', REMOTE_ADDR='169.254.100.6', **headers)

    @override_settings(TRUST_VERCEL_IP_HEADERS=True, DEBUG=False)
    def test_each_visitor_gets_their_own_address_on_vercel(self):
        self.assertEqual(client_ip(self.request(HTTP_X_VERCEL_FORWARDED_FOR='203.0.113.7')), '203.0.113.7')
        self.assertEqual(client_ip(self.request(HTTP_X_REAL_IP='198.51.100.23')), '198.51.100.23')
        self.assertEqual(client_ip(self.request(HTTP_X_VERCEL_FORWARDED_FOR='2001:db8::1, 10.0.0.1')), '2001:db8::1')

    @override_settings(TRUST_VERCEL_IP_HEADERS=True, DEBUG=False)
    def test_garbage_headers_fall_back_to_the_connection_address(self):
        self.assertEqual(client_ip(self.request(HTTP_X_VERCEL_FORWARDED_FOR='not-an-ip')), '169.254.100.6')

    @override_settings(TRUST_VERCEL_IP_HEADERS=False, DEBUG=False)
    def test_the_headers_are_ignored_anywhere_else(self):
        self.assertEqual(client_ip(self.request(HTTP_X_VERCEL_FORWARDED_FOR='203.0.113.7', HTTP_X_REAL_IP='203.0.113.8')),
                         '169.254.100.6')
