"""Dark, Light and Auto appearance: the switch, the cookie it sets, and the token sets behind it."""
import re
from pathlib import Path

from django.conf import settings
from django.test import Client
from django.urls import reverse

from tracker.appearance import THEME_COOKIE
from tracker.tests.test_real_world import Scenario

TOKENS = Path(settings.BASE_DIR) / 'static/tracker/tokens.css'


def html_theme(response):
    return re.search(r'<html lang="en" data-theme="([^"]*)">', response.content.decode()).group(1)


def color_scheme(response):
    return re.search(r'<meta name="color-scheme" content="([^"]*)">', response.content.decode()).group(1)


def pressed(response):
    return re.findall(r'name="theme" value="(\w+)"[^>]*aria-pressed="true"', response.content.decode())


class AppearanceTests(Scenario):
    def choose(self, client, theme, next_url='', **extra):
        return client.post(reverse('appearance'), {'theme': theme, 'next': next_url}, **extra)

    def test_dark_is_the_default_for_everyone(self):
        for client, url in ((self.manager, reverse('dashboard')), (self.signed_in(self.anthony), reverse('dashboard')), (Client(), reverse('login'))):
            page = client.get(url)
            self.assertEqual((html_theme(page), color_scheme(page), pressed(page)), ('dark', 'dark', ['dark']))

    def test_choosing_light_is_remembered_and_returns_to_the_same_page(self):
        here = reverse('templates_index') + '?week=2026-10-05'
        response = self.choose(self.manager, 'light', here)
        self.assertRedirects(response, here, fetch_redirect_response=False)
        cookie = response.cookies[THEME_COOKIE]
        self.assertEqual(cookie.value, 'light')
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')
        self.assertGreater(int(cookie['max-age']), 300 * 24 * 60 * 60)
        for name in ('dashboard', 'templates_index', 'texting_lists_index', 'team', 'audit_log', 'upload_new'):
            page = self.manager.get(reverse(name))
            self.assertEqual((html_theme(page), color_scheme(page), pressed(page)), ('light', 'light', ['light']), name)

    def test_auto_follows_the_device(self):
        self.choose(self.manager, 'system')
        page = self.manager.get(reverse('dashboard'))
        self.assertEqual((html_theme(page), color_scheme(page), pressed(page)), ('system', 'dark light', ['system']))

    def test_reps_and_signed_out_visitors_can_switch_too(self):
        rep = self.signed_in(self.anthony)
        self.choose(rep, 'light')
        self.assertEqual(html_theme(rep.get(reverse('dashboard'))), 'light')
        visitor = Client()
        response = self.choose(visitor, 'light', reverse('login'))
        self.assertRedirects(response, reverse('login'), fetch_redirect_response=False)
        login_page = visitor.get(reverse('login'))
        self.assertEqual((html_theme(login_page), pressed(login_page)), ('light', ['light']))

    def test_the_choice_survives_signing_out(self):
        self.choose(self.manager, 'light')
        self.manager.post(reverse('logout'))
        self.assertEqual(html_theme(self.manager.get(reverse('login'))), 'light')

    def test_unknown_or_tampered_values_fall_back_to_dark(self):
        response = self.choose(self.manager, '"><script>alert(1)</script>')
        self.assertNotIn(THEME_COOKIE, response.cookies)
        self.manager.cookies[THEME_COOKIE] = '"><script>alert(1)</script>'
        page = self.manager.get(reverse('dashboard'))
        self.assertEqual((html_theme(page), color_scheme(page)), ('dark', 'dark'))
        self.assertNotContains(page, '<script>alert(1)')

    def test_offsite_return_addresses_are_ignored(self):
        for next_url in ('https://evil.example/', '//evil.example/x', 'javascript:alert(1)'):
            response = self.choose(self.manager, 'light', next_url)
            self.assertRedirects(response, reverse('dashboard'), fetch_redirect_response=False)

    def test_background_save_answers_json_without_redirecting(self):
        response = self.choose(self.manager, 'light', reverse('audit_log'), HTTP_ACCEPT='application/json')
        self.assertEqual((response.status_code, response.json()), (200, {'theme': 'light'}))
        self.assertEqual(response.cookies[THEME_COOKIE].value, 'light')
        bad = self.choose(self.manager, 'neon', HTTP_ACCEPT='application/json')
        self.assertEqual(bad.status_code, 400)
        self.assertNotIn(THEME_COOKIE, bad.cookies)

    def test_switching_needs_post_and_a_csrf_token(self):
        self.assertEqual(self.manager.get(reverse('appearance')).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(self.choose(strict, 'light').status_code, 403)

    def test_switching_never_touches_the_audit_history_or_session(self):
        from tracker.models import AuditEvent
        before = AuditEvent.objects.count()
        session_key = self.manager.session.session_key
        self.choose(self.manager, 'light')
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertEqual(self.manager.session.session_key, session_key)

    def test_light_and_auto_token_sets_match_and_cover_every_dark_color(self):
        css = TOKENS.read_text()
        dark, rest = css.split(':root[data-theme="light"]', 1)
        light, auto = rest.split('@media (prefers-color-scheme: light)', 1)

        def colors(block):
            return dict(re.findall(r'--((?:color|gradient|shadow)-[\w-]+):\s*([^;]+);', block))
        self.assertEqual(colors(light), colors(auto))
        self.assertEqual(set(colors(dark)), set(colors(light)))
        self.assertIn('color-scheme: light', light)
        self.assertIn(':root[data-theme="system"]', auto)
        # Cool neutrals only: the page and surfaces never lean warm (red above blue reads as cream).
        for name in ('color-bg', 'color-sidebar', 'color-surface', 'color-surface-2', 'color-surface-3'):
            red, blue = int(colors(light)[name][1:3], 16), int(colors(light)[name][5:7], 16)
            self.assertLessEqual(red, blue, name)
        self.assertEqual((Path(settings.BASE_DIR) / 'tokens.css').read_text(), css)
