"""Admin-managed texting lists: create, rename, change reps, hide and delete."""
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tracker.models import ImportBatch, RepListMembership, ScheduleTemplate, TemplateList, TextingList, TextingListType
from tracker.planning import week_start
from tracker.services import import_workbook, publish_batches
from tracker.tests.test_real_world import Scenario, fake_numbers, xlsx


class TextingListTests(Scenario):
    def create(self, name, nickname='', reps=()):
        response = self.manager.post(reverse('texting_lists_index'), {'name': name, 'nickname': nickname, 'reps': [rep.pk for rep in reps]})
        return response, TextingList.objects.filter(name=name).first()

    def members(self, item):
        return set(RepListMembership.objects.filter(list_type=item.key).values_list('rep__username', flat=True))

    def test_the_two_original_lists_are_kept(self):
        self.assertEqual({(i.key, i.label, i.short_label) for i in TextingList.objects.all()}, {
            ('gfs', 'GFS Texting (Donut)', 'GFS · Donut'), ('ringcentral', 'Ringcentral Texting (Clean)', 'Ringcentral · Clean')})

    def test_admin_creates_a_list_and_sends_a_file_through_it(self):
        response, item = self.create('SMS Magic', 'Hot', [self.anthony, self.blake])
        self.assertRedirects(response, item.get_absolute_url())
        self.assertEqual((item.key, item.label, item.short_label), ('sms-magic', 'SMS Magic (Hot)', 'SMS Magic · Hot'))
        self.assertEqual(self.members(item), {'anthony.diaz', 'blake.fiorito'})
        self.assertContains(self.manager.get(reverse('upload_new')), 'SMS Magic (Hot)')

        upload = self.upload_via_ui(xlsx(fake_numbers(6, '646')), label='Magic Monday', list_type=item.key)
        review = self.manager.get(upload.get_absolute_url())
        self.assertEqual(set(review.context['form'].fields['reps'].queryset), {self.anthony, self.blake})
        self.assertEqual(self.split_via_ui(upload, [self.today], [self.anthony, self.blake]).status_code, 302)
        rep_home = self.signed_in(self.anthony).get(reverse('dashboard'))
        self.assertContains(rep_home, 'SMS Magic (Hot)')
        self.assertContains(rep_home, 'SMS Magic · Hot')
        self.assertContains(self.manager.get(reverse('audit_log')), 'Texting list created')

    def test_renaming_relabels_history_without_changing_it(self):
        _, batches = self.assign(4, [self.today], [self.blake], label='Donut day', list_type='gfs')
        gfs = TextingList.objects.get(key='gfs')
        self.manager.post(gfs.get_absolute_url(), {'action': 'update', 'name': 'GFS Prime', 'nickname': 'Donut', 'is_active': 'on'})
        gfs.refresh_from_db()
        self.assertEqual((gfs.key, gfs.label), ('gfs', 'GFS Prime (Donut)'))
        self.assertContains(self.manager.get(batches[0].upload.get_absolute_url()), 'GFS Prime (Donut)')
        self.assertContains(self.signed_in(self.blake).get(reverse('dashboard')), 'GFS Prime · Donut')
        self.assertEqual(ImportBatch.objects.get(pk=batches[0].upload_id).list_type, 'gfs')

    def test_adding_and_removing_reps_changes_access_right_away(self):
        _, item = self.create('Weekend push', reps=[self.anthony])
        upload = import_workbook(xlsx(fake_numbers(2, '917')), self.admin, 'Push batch A', list_type=item.key)
        batch = publish_batches(upload, [self.today], [self.anthony], self.admin)[0]
        rep = self.signed_in(self.anthony)
        self.assertEqual(self.shown(rep, batch), self.numbers(batch))
        self.manager.post(item.get_absolute_url(), {'action': 'members', 'reps': [self.erik.pk]})
        self.assertEqual(self.members(item), {'erik.anderson'})
        self.assertEqual(rep.get(batch.get_absolute_url()).status_code, 404)
        self.assertNotContains(rep.get(reverse('dashboard')), 'Push batch A')
        self.manager.post(item.get_absolute_url(), {'action': 'members', 'reps': [self.erik.pk, self.anthony.pk]})
        self.assertEqual(self.shown(rep, batch), self.numbers(batch))
        self.assertContains(self.manager.get(reverse('audit_log')), 'Texting list reps changed')

    def test_hidden_lists_leave_the_pickers_but_old_lists_keep_working(self):
        _, batches = self.assign(2, [self.today], [self.blake], label='Before hiding', list_type='gfs')
        template = ScheduleTemplate.objects.create(name='Week', is_active=True, created_by=self.admin)
        TemplateList.objects.create(template=template, label='Donut daily', list_type='gfs', weekdays=list(range(7)))
        gfs = TextingList.objects.get(key='gfs')
        self.manager.post(gfs.get_absolute_url(), {'action': 'update', 'name': gfs.name, 'nickname': gfs.nickname})
        self.assertFalse(TextingList.objects.get(key='gfs').is_active)

        self.assertNotContains(self.manager.get(reverse('upload_new')), 'GFS Texting (Donut)')
        refused = self.manager.post(reverse('upload_new'), {'label': 'x', 'list_type': 'gfs', 'file': xlsx(fake_numbers(1, '718'))})
        self.assertIn('list_type', refused.context['form'].errors)
        self.assertNotContains(self.manager.get(template.get_absolute_url()), '<option value="gfs"')
        self.assertNotContains(self.manager.get(reverse('team_detail', args=[self.emilio.pk])), 'value="gfs"')
        self.manager.post(reverse('team_detail', args=[self.emilio.pk]), {'action': 'update_memberships', 'list_types': ['ringcentral']})
        self.assertEqual(set(self.emilio.list_memberships.values_list('list_type', flat=True)), {'gfs', 'ringcentral'})
        next_week = week_start(self.today) + timedelta(weeks=1)
        calendar = self.manager.get(reverse('dashboard'), {'date': next_week.isoformat()}).context['calendar_days']
        self.assertFalse(any(day['planned'] for day in calendar))
        self.assertEqual(self.shown(self.signed_in(self.blake), batches[0]), self.numbers(batches[0]))

        self.manager.post(gfs.get_absolute_url(), {'action': 'update', 'name': gfs.name, 'nickname': gfs.nickname, 'is_active': 'on'})
        self.assertContains(self.manager.get(reverse('upload_new')), 'GFS Texting (Donut)')

    def test_only_lists_without_history_can_be_deleted(self):
        _, unused = self.create('Trial list', reps=[self.anthony, self.erik])
        page = self.manager.get(unused.get_absolute_url())
        self.assertContains(page, 'Delete texting list')
        self.assertRedirects(self.manager.post(unused.get_absolute_url(), {'action': 'delete'}), reverse('texting_lists_index'))
        self.assertFalse(TextingList.objects.filter(name='Trial list').exists())
        self.assertFalse(RepListMembership.objects.filter(list_type=unused.key).exists())
        self.assertTrue(self.anthony.list_memberships.filter(list_type='ringcentral').exists())

        self.assign(1, [self.today], [self.blake], list_type='gfs')
        gfs = TextingList.objects.get(key='gfs')
        self.assertNotContains(self.manager.get(gfs.get_absolute_url()), 'Delete texting list')
        response = self.manager.post(gfs.get_absolute_url(), {'action': 'delete'}, follow=True)
        self.assertContains(response, 'can’t be deleted')
        self.assertTrue(TextingList.objects.filter(key='gfs').exists())

    def test_names_are_unique_safe_and_get_their_own_keys(self):
        duplicate, _ = self.create('  gfs   TEXTING ')
        self.assertIn('already exists', str(duplicate.context['form'].non_field_errors()))
        _, first = self.create('SMS Magic')
        _, second = self.create('SMS-Magic!')
        self.assertEqual((first.key, second.key), ('sms-magic', 'sms-magic-2'))
        _, sneaky = self.create('<img src=x onerror=alert(1)>', nickname='<b>x</b>')
        for url in (reverse('texting_lists_index'), sneaky.get_absolute_url(), reverse('upload_new'), reverse('team')):
            self.assertNotIn('<img src=x onerror', self.manager.get(url).content.decode())
        empty, _ = self.create('   ')
        self.assertTrue(empty.context['form'].errors)
        staff, _ = self.create('With admin', reps=[self.admin])
        self.assertIn('reps', staff.context['form'].errors)
        self.assertFalse(TextingList.objects.filter(name='With admin').exists())

    def test_new_lists_show_up_for_new_reps_and_templates(self):
        _, item = self.create('SMS Magic', 'Hot')
        response = self.manager.post(reverse('team'), {'action': 'create', 'first_name': 'Nia', 'last_name': 'Lopez',
                                                       'username': 'nia.lopez', 'list_types': [item.key]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.members(item), {'nia.lopez'})
        template = ScheduleTemplate.objects.create(name='Week', created_by=self.admin)
        self.assertContains(self.manager.get(template.get_absolute_url()), f'<option value="{item.key}">SMS Magic (Hot)</option>', html=False)

    def test_reps_cannot_manage_texting_lists(self):
        _, item = self.create('Private list', reps=[self.anthony])
        rep = self.signed_in(self.anthony)
        for method, url, data in [('get', reverse('texting_lists_index'), {}), ('post', reverse('texting_lists_index'), {'name': 'Mine'}),
                                  ('get', item.get_absolute_url(), {}), ('post', item.get_absolute_url(), {'action': 'delete'}),
                                  ('post', item.get_absolute_url(), {'action': 'members', 'reps': [self.anthony.pk, self.erik.pk]})]:
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(rep, method)(url, data).status_code, 403)
        self.assertEqual(self.members(item), {'anthony.diaz'})
        self.assertFalse(TextingList.objects.filter(name='Mine').exists())
        self.assertNotContains(rep.get(reverse('dashboard')), reverse('texting_lists_index'))

    def test_list_names_cost_one_lookup_per_page(self):
        for number in range(6):
            self.assign(2, [self.today + timedelta(days=number % 3)], [self.anthony], label=f'List {number}',
                        area=['212', '312', '415', '646', '718', '773'][number])
        with CaptureQueriesContext(connection) as queries:
            response = self.manager.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sum('tracker_textinglist' in q['sql'] for q in queries.captured_queries), 1)
