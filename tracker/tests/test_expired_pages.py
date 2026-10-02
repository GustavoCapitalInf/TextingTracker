"""Out-of-date forms (another tab, the Back button, a page left open) recover instead of showing a bare 403."""
import re

from django.test import Client
from django.urls import reverse

from tracker.models import TextTemplate
from tracker.tests.test_real_world import PASSWORD, Scenario

LOGIN = reverse('login')


def token(response):
    return re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', response.content.decode()).group(1)


class ExpiredPageTests(Scenario):
    def browser(self):
        return Client(enforce_csrf_checks=True)

    def test_a_sign_in_page_from_before_another_sign_in_takes_you_in(self):
        """What happened live: the old tab's code was renewed by a sign-in in another tab."""
        browser = self.browser()
        old_tab = token(browser.get(LOGIN))
        new_tab = token(browser.get(LOGIN))
        self.assertEqual(browser.post(LOGIN, {'username': 'anthony.diaz', 'password': PASSWORD, 'csrfmiddlewaretoken': new_tab}).status_code, 302)
        stale = browser.post(LOGIN, {'username': 'anthony.diaz', 'password': PASSWORD, 'csrfmiddlewaretoken': old_tab}, follow=True)
        self.assertEqual(stale.redirect_chain, [(f'{LOGIN}?expired=1', 302), (reverse('dashboard'), 302)])
        self.assertEqual(stale.status_code, 200)

    def test_a_stale_sign_in_page_reloads_with_a_note_and_the_next_try_works(self):
        browser = self.browser()
        browser.get(LOGIN)
        browser.cookies['csrftoken'] = 'A' * 32   # the browser's code changed after the page loaded
        response = browser.post(LOGIN, {'username': 'anthony.diaz', 'password': PASSWORD, 'csrfmiddlewaretoken': 'B' * 64,
                                        'next': '/templates/'})
        self.assertRedirects(response, f'{LOGIN}?expired=1&next=%2Ftemplates%2F', fetch_redirect_response=False)
        page = browser.get(response.url)
        self.assertContains(page, 'That sign-in page was out of date, so it was refreshed.')
        signed_in = browser.post(response.url, {'username': 'anthony.diaz', 'password': PASSWORD, 'csrfmiddlewaretoken': token(page),
                                                'next': '/templates/'})
        self.assertRedirects(signed_in, '/templates/', fetch_redirect_response=False)

    def test_the_reload_never_sends_people_off_site(self):
        browser = self.browser()
        browser.get(LOGIN)
        browser.cookies['csrftoken'] = 'A' * 32
        response = browser.post(LOGIN, {'username': 'x', 'password': 'y', 'csrfmiddlewaretoken': 'B' * 64, 'next': 'https://evil.example/'})
        self.assertRedirects(response, f'{LOGIN}?expired=1', fetch_redirect_response=False)

    def test_a_plain_visit_shows_no_expired_note(self):
        self.assertNotContains(Client().get(LOGIN), 'out of date')
        self.assertNotContains(Client().get(LOGIN, {'expired': '2'}), 'out of date')

    def test_other_out_of_date_forms_explain_and_change_nothing(self):
        browser = self.browser()
        browser.force_login(self.admin)
        browser.get(reverse('text_templates'))
        response = browser.post(reverse('text_templates'), {'name': 'Stale', 'body': 'Stale', 'csrfmiddlewaretoken': 'B' * 64})
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'This page expired', status_code=403)
        self.assertContains(response, 'Nothing was changed.', status_code=403)
        self.assertContains(response, '<link rel="stylesheet"', status_code=403)   # the app's own page, not a bare error
        self.assertFalse(TextTemplate.objects.exists())

    def test_background_saves_get_a_short_answer(self):
        browser = self.browser()
        response = browser.post(reverse('appearance'), {'theme': 'light'}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertIn('Nothing was changed.', response.json()['error'])
