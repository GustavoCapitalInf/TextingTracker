"""Text templates: admins manage ready-made texts; reps read and copy them; none ship with the app."""
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from tracker import text_templates
from tracker.models import AuditEvent, TextTemplate
from tracker.tests.test_real_world import Scenario

LIST = reverse('text_templates')
FOLLOW_UP = 'Hi! Just following up on the funding options we sent.\nReply YES and I’ll call you today.'


class TextTemplateScenario(Scenario):
    def add(self, name='Follow-up', body=FOLLOW_UP, client=None):
        return (client or self.manager).post(LIST, {'name': name, 'body': body})

    def edit(self, item, **data):
        return self.manager.post(item.get_absolute_url(), {'action': 'update', 'name': item.name, 'body': item.body, **data})


class NothingShipsTests(TextTemplateScenario):
    def test_a_fresh_install_has_no_templates_and_says_so(self):
        self.assertFalse(TextTemplate.objects.exists())
        self.assertContains(self.manager.get(LIST), 'No templates yet')
        self.assertContains(self.signed_in(self.anthony).get(LIST), 'Your admin hasn’t added any templates.')


class AdminTemplateTests(TextTemplateScenario):
    def test_admin_adds_a_template_and_every_rep_sees_it_exactly(self):
        response = self.add(body='  ' + FOLLOW_UP.replace('\n', '\r\n') + '\n\n')
        self.assertRedirects(response, LIST)
        item = TextTemplate.objects.get()
        self.assertEqual((item.name, item.body, item.created_by), ('Follow-up', FOLLOW_UP, self.admin))
        for rep in (self.anthony, self.blake, self.emilio):
            page = self.signed_in(rep).get(LIST)
            self.assertContains(page, 'Follow-up')
            self.assertContains(page, 'Hi! Just following up on the funding options we sent.\nReply YES and I’ll call you today.')
            self.assertContains(page, f'data-copy-target="text-template-{item.pk}" hidden>Copy text</button>')
        self.assertContains(self.manager.get(reverse('audit_log')), 'Text template added')

    def test_names_and_text_are_checked(self):
        self.add('Follow-up')
        for name, body, error in [('', 'Text', 'This field is required.'), ('   ', 'Text', 'This field is required.'),
                                  ('Intro', '   \n  ', 'Add the text reps should send.'), ('x' * 81, 'Text', 'at most 80 characters'),
                                  ('Intro', 'x' * 1601, 'at most 1600 characters'), ('  FOLLOW-UP ', 'Other text', 'already exists')]:
            with self.subTest(name=name[:12], body=body[:12]):
                response = self.add(name, body)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, error)
        self.assertEqual(TextTemplate.objects.count(), 1)

    def test_the_database_refuses_duplicate_names_too(self):
        TextTemplate.objects.create(name='Intro', body='Hello')
        with self.assertRaises(IntegrityError), transaction.atomic():
            TextTemplate.objects.create(name='INTRO', body='Hi')

    def test_admin_edits_and_deletes_a_template(self):
        self.add('Follow-up')
        self.add('Intro', 'Hello from Capital Infusion.')
        item = TextTemplate.objects.get(name='Follow-up')
        self.assertRedirects(self.edit(item, name='Second follow-up', body='Still interested?'), LIST)
        item.refresh_from_db()
        self.assertEqual((item.name, item.body), ('Second follow-up', 'Still interested?'))
        self.assertContains(self.edit(item, name='intro'), 'already exists')
        self.assertEqual(self.edit(item, name='Second follow-up', body='Still interested? Reply YES.').status_code, 302)
        self.assertRedirects(self.manager.post(item.get_absolute_url(), {'action': 'delete'}), LIST)
        self.assertEqual(list(TextTemplate.objects.values_list('name', flat=True)), ['Intro'])
        self.assertNotContains(self.signed_in(self.anthony).get(LIST), 'Still interested')
        actions = list(AuditEvent.objects.filter(action__startswith='text_template.').values_list('action', flat=True))
        self.assertEqual(sorted(actions), ['text_template.created', 'text_template.created', 'text_template.deleted',
                                           'text_template.updated', 'text_template.updated'])
        history = self.manager.get(reverse('audit_log'))
        for text in ('Text template saved', 'Changed the name and text of the “Second follow-up” template.',
                     'Deleted the “Second follow-up” template; reps no longer see it.'):
            self.assertContains(history, text)

    def test_templates_are_listed_by_name(self):
        for name in ('Zero interest', 'after hours', 'Busy signal'):
            self.add(name, f'{name} text')
        page = self.signed_in(self.blake).get(LIST)
        self.assertEqual([item.name for item in page.context['items']], ['after hours', 'Busy signal', 'Zero interest'])


class RepTemplateTests(TextTemplateScenario):
    def setUp(self):
        super().setUp()
        self.add()
        self.item = TextTemplate.objects.get()
        self.rep = self.signed_in(self.anthony)

    def test_reps_read_but_never_manage(self):
        page = self.rep.get(LIST)
        self.assertNotContains(page, 'Add a template')
        self.assertNotContains(page, self.item.get_absolute_url())
        self.assertNotContains(page, '<form method="post" class="panel-body form-stack">')
        self.assertEqual(self.add('Rep made this', 'Hijack', client=self.rep).status_code, 403)
        for data in ({'action': 'update', 'name': 'Hacked', 'body': 'Hacked'}, {'action': 'delete'}, {}):
            self.assertEqual(self.rep.post(self.item.get_absolute_url(), data).status_code, 403)
        self.assertEqual(self.rep.get(self.item.get_absolute_url()).status_code, 403)
        self.item.refresh_from_db()
        self.assertEqual((TextTemplate.objects.count(), self.item.name), (1, 'Follow-up'))

    def test_reps_without_any_texting_list_still_see_templates(self):
        rep = self.make_rep('new.hire', 'New', 'Hire')
        self.assertContains(self.signed_in(rep).get(LIST), 'Follow-up')

    def test_signed_out_visitors_are_sent_to_sign_in(self):
        for url in (LIST, self.item.get_absolute_url()):
            response = Client().get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse('login'), response.url)

    def test_posting_needs_a_csrf_token(self):
        forged = Client(enforce_csrf_checks=True)
        forged.force_login(self.admin)
        self.assertEqual(forged.post(LIST, {'name': 'Forged', 'body': 'Forged'}).status_code, 403)
        self.assertFalse(TextTemplate.objects.filter(name='Forged').exists())

    def test_template_text_is_shown_as_text_not_html(self):
        text_templates.create_template(self.admin, name='<b>Bold</b>', body='<script>alert(1)</script> & "quotes"')
        page = self.rep.get(LIST)
        self.assertNotContains(page, '<script>alert(1)')
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quotes&quot;')
        self.assertContains(page, '&lt;b&gt;Bold&lt;/b&gt;')

    def test_service_layer_refuses_reps(self):
        from django.core.exceptions import PermissionDenied, ValidationError
        with self.assertRaises((PermissionDenied, ValidationError)):
            text_templates.create_template(self.anthony, name='Nope', body='Nope')


class NavigationTests(TextTemplateScenario):
    def test_admins_get_calendar_management_and_templates_reps_get_templates(self):
        admin_page = self.manager.get(reverse('dashboard')).content.decode()
        rep_page = self.signed_in(self.anthony).get(reverse('dashboard')).content.decode()
        self.assertIn('<span>Calendar Management</span>', admin_page)
        self.assertIn(f'href="{LIST}"', admin_page)
        self.assertIn(f'href="{LIST}"', rep_page)
        self.assertNotIn('Calendar Management', rep_page)
        self.assertEqual(reverse('calendar_management'), '/calendar-management/')
        self.assertContains(self.manager.get(reverse('calendar_management')), '<h1>Calendar Management</h1>')
        self.assertEqual(self.signed_in(self.anthony).get(reverse('calendar_management')).status_code, 403)
