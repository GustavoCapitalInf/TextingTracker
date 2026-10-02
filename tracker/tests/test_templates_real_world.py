"""Templates the way people really use them, and the ways people try to bend them."""
import re
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Count
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tracker.models import AuditEvent, ImportBatch, PlanSkip, RepBatch, ScheduleTemplate, TemplateList
from tracker.planning import week_start
from tracker.services import close_upload, import_workbook
from tracker.tests.test_real_world import company_time, xlsx
from tracker.tests.test_templates import TemplateScenario, unique_numbers


def messy_column(numbers):
    """The same numbers typed the many ways people type them, with no header row."""
    digits = [re.sub(r'\D', '', number) for number in numbers]
    styles = [
        lambda d: f'({d[:3]}) {d[3:6]}-{d[6:]}', lambda d: f'{d[:3]}.{d[3:6]}.{d[6:]}',
        lambda d: f'{d[:3]}–{d[3:6]}–{d[6:]}', lambda d: f'+1 {d[:3]} {d[3:6]} {d[6:]}', lambda d: int(d),
        lambda d: f'1-{d[:3]}-{d[3:6]}-{d[6:]}', lambda d: f'{d[:3]} {d[3:6]} {d[6:]}',
    ]
    return [styles[index % len(styles)](d) for index, d in enumerate(digits)]


class TemplateJourneyTests(TemplateScenario):
    def setUp(self):
        super().setUp()
        self.this_week = week_start(self.today)
        self.next_monday = self.this_week + timedelta(weeks=1)
        self.next_week = self.week_days(self.next_monday)

    def normal_month(self):
        for back in (3, 2, 1):
            week = self.this_week - timedelta(weeks=back)
            days = self.week_days(week)
            self.sent('SMS Magic Response- clean', 'ringcentral', [self.anthony, self.erik], [week])
            self.sent('organic text- Clean', 'ringcentral', [self.anthony, self.erik], days[1:5])
            self.sent('Donut daily', 'gfs', [self.blake, self.emilio], days[:5])

    def draft_and_turn_on(self, name='Our normal week'):
        self.manager.post(reverse('templates_index'), {'action': 'suggest'})
        template = ScheduleTemplate.objects.latest('pk')
        self.manager.post(template.get_absolute_url(), {'action': 'update_template', 'name': name, 'is_active': 'on'})
        return template

    def upload_planned(self, slot, file, label=None, list_type=None, week=None):
        response = self.manager.post(reverse('upload_new'), {
            'label': label or slot.label, 'list_type': list_type or slot.list_type, 'file': file,
            'slot': slot.pk, 'week': (week or self.next_monday).isoformat()})
        self.assertEqual(response.status_code, 302, response.context and response.context['form'].errors)
        return ImportBatch.objects.get(pk=response.url.strip('/').split('/')[-1])

    def create_from_prefill(self, upload):
        initial = self.manager.get(upload.get_absolute_url()).context['form'].initial
        return self.manager.post(reverse('upload_publish', args=[upload.pk]), {
            'dates': initial['dates'], 'rep_count': initial['rep_count'], 'reps': initial['reps'], 'intent': 'create'})

    def test_a_month_of_normal_weeks_then_the_template_runs_the_next_one(self):
        self.normal_month()
        template = self.draft_and_turn_on()
        self.assertEqual(sorted((slot.label, slot.weekday_label) for slot in template.lists.all()),
                         [('Donut daily', 'Mon–Fri'), ('SMS Magic Response- clean', 'Mon'), ('organic text- Clean', 'Tue–Fri')])
        planned = self.planned(self.manager, self.next_monday)
        self.assertEqual(sorted(planned[self.next_week[0]]), [('Donut daily', 'needs_file'), ('SMS Magic Response- clean', 'needs_file')])
        self.assertEqual(sorted(planned[self.next_week[2]]), [('Donut daily', 'needs_file'), ('organic text- Clean', 'needs_file')])

        organic = template.lists.get(label='organic text- Clean')
        numbers = unique_numbers(12)
        upload = self.upload_planned(organic, xlsx([*messy_column(numbers), None, numbers[0], 'N/A', '555-0199']))
        counts = dict(upload.rows.values_list('classification').annotate(n=Count('pk')))
        self.assertEqual(counts, {'ready': 12, 'duplicate': 1, 'invalid': 2})
        self.assertEqual(self.create_from_prefill(upload).status_code, 302)
        self.assertEqual(sorted(set(RepBatch.objects.filter(upload=upload).values_list('scheduled_date', flat=True))),
                         self.next_week[1:5])

        with company_time(datetime.combine(self.next_week[2], time(9))):
            anthony = self.signed_in(self.anthony)
            todays = anthony.get(reverse('dashboard')).context['batches']
            self.assertEqual([batch.upload.label for batch in todays], ['organic text- Clean'])
            self.assertEqual(self.shown(anthony, todays[0]), self.numbers(todays[0]))
            self.assertEqual(self.signed_in(self.blake).get(reverse('dashboard')).context['batches'], [])

        planned = self.planned(self.manager, self.next_monday)
        self.assertEqual(planned[self.next_week[2]], [('Donut daily', 'needs_file')])
        following = self.planned(self.manager, self.next_monday + timedelta(weeks=1))
        self.assertIn(('organic text- Clean', 'needs_file'), following[self.next_week[2] + timedelta(weeks=1)])

    def test_a_planned_file_with_previously_sent_numbers_asks_first_then_prefills(self):
        self.normal_month()
        template = self.draft_and_turn_on()
        organic = template.lists.get(label='organic text- Clean')
        already_sent = list(RepBatch.objects.filter(upload__label='organic text- Clean').values_list(
            'assignments__phone__canonical', flat=True)[:3])
        upload = self.upload_planned(organic, xlsx([*already_sent, *unique_numbers(5)]))
        review = self.manager.get(upload.get_absolute_url())
        self.assertTrue(review.context['needs_previous_decision'])
        self.assertNotContains(review, 'data-split-form')
        self.manager.post(reverse('upload_previous', args=[upload.pk]), {'include': 'no'})
        review = self.manager.get(upload.get_absolute_url())
        self.assertEqual(review.context['assignable_count'], 5)
        self.assertEqual(review.context['form'].initial['dates'].split('\n'), [d.isoformat() for d in self.next_week[1:5]])

    def test_clearing_a_planned_list_puts_it_back_on_the_plan(self):
        _, _, organic = self.template()
        upload = self.upload_planned(organic, xlsx(unique_numbers(8)))
        self.create_from_prefill(upload)
        self.assertEqual(self.planned(self.manager, self.next_monday)[self.next_week[2]], [])
        close_upload(upload, self.admin)
        self.assertEqual(self.planned(self.manager, self.next_monday)[self.next_week[2]], [('organic text- Clean', 'needs_file')])

    def test_changing_the_plan_midweek_updates_the_calendar(self):
        template, _, organic = self.template()
        self.manager.post(template.get_absolute_url(), {
            'action': 'save_list', 'list_id': organic.pk, f'list-{organic.pk}-label': 'organic text- Clean',
            f'list-{organic.pk}-list_type': 'ringcentral', f'list-{organic.pk}-weekdays': [0, 1, 2, 3],
            f'list-{organic.pk}-reps': [self.anthony.pk]})
        planned = self.planned(self.manager, self.next_monday)
        self.assertIn(('organic text- Clean', 'needs_file'), planned[self.next_week[0]])
        self.assertEqual(planned[self.next_week[4]], [])

    def test_admin_switches_the_group_at_upload(self):
        _, _, organic = self.template()
        upload = self.upload_planned(organic, xlsx(unique_numbers(4)), list_type='gfs')
        review = self.manager.get(upload.get_absolute_url())
        self.assertNotIn('reps', review.context['form'].initial)
        self.assertTrue(any('left out' in note for note in review.context['plan_notes']))

    def test_choosing_only_tuesday_leaves_wednesday_to_friday_planned(self):
        _, _, organic = self.template()
        tuesday_only = self.upload_planned(organic, xlsx(unique_numbers(4)))
        self.split_via_ui(tuesday_only, [self.next_week[1]], [self.anthony])
        planned = self.planned(self.manager, self.next_monday)
        self.assertEqual(planned[self.next_week[1]], [])
        for day in self.next_week[2:5]:
            self.assertEqual(planned[day], [('organic text- Clean', 'needs_file')])

        link = {'slot': organic.pk, 'week': self.next_monday.isoformat()}
        self.assertContains(self.manager.get(reverse('upload_new'), link), 'already uploaded')
        rest_of_week = self.upload_planned(organic, xlsx(unique_numbers(6)))
        review = self.manager.get(rest_of_week.get_absolute_url())
        self.assertEqual(review.context['form'].initial['dates'].split('\n'), [day.isoformat() for day in self.next_week[2:5]])
        self.assertContains(review, 'Days that already have this list, or were skipped, were left out.')
        self.create_from_prefill(rest_of_week)
        self.assertFalse(any(self.planned(self.manager, self.next_monday)[day] for day in self.next_week[1:5]))

    def test_skipping_a_day_hides_it_until_undone(self):
        _, _, organic = self.template()
        other, _, twin = self.template(name='Same lists, second template')
        TemplateList.objects.create(template=other, label='Donut daily', list_type='gfs', weekdays=[2])
        wednesday = self.next_week[2]
        response = self.manager.post(reverse('plan_skip'), {'slot': organic.pk, 'day': wednesday.isoformat()})
        self.assertRedirects(response, reverse('dashboard') + f'?date={wednesday.isoformat()}')
        planned = self.planned(self.manager, self.next_monday)
        self.assertEqual(planned[wednesday], [('Donut daily', 'needs_file')])  # other lists that day stay planned
        self.assertEqual(planned[self.next_week[3]], [('organic text- Clean', 'needs_file')])
        page = self.manager.get(reverse('dashboard'), {'date': wednesday.isoformat()})
        self.assertContains(page, 'Skipped this week:')
        upload = self.upload_planned(twin, xlsx(unique_numbers(4)))
        dates = self.manager.get(upload.get_absolute_url()).context['form'].initial['dates'].split('\n')
        self.assertNotIn(wednesday.isoformat(), dates)
        skip = PlanSkip.objects.get()
        self.manager.post(reverse('plan_unskip', args=[skip.pk]))
        self.assertIn(('organic text- Clean', 'uploaded'), self.planned(self.manager, self.next_monday)[wednesday])
        self.assertContains(self.manager.get(reverse('audit_log')), 'Planned day skipped')

    def test_clearing_one_day_brings_that_day_back(self):
        _, _, organic = self.template()
        upload = self.upload_planned(organic, xlsx(unique_numbers(8)))
        self.create_from_prefill(upload)
        for batch in RepBatch.objects.filter(upload=upload, scheduled_date=self.next_week[3]):
            self.manager.post(reverse('batch_close', args=[batch.pk]))
        planned = self.planned(self.manager, self.next_monday)
        self.assertEqual(planned[self.next_week[3]], [('organic text- Clean', 'needs_file')])
        self.assertEqual(planned[self.next_week[2]], [])

    def test_the_plan_rolls_over_at_midnight_company_time(self):
        self.template()
        sunday = self.next_monday - timedelta(days=1)
        tuesday = self.next_week[1]
        with company_time(datetime.combine(sunday, time(23, 59))):
            sunday_view = self.planned(self.signed_in(self.admin), self.next_monday)
        with company_time(datetime.combine(tuesday, time(0, 1))):
            tuesday_view = self.planned(self.signed_in(self.admin), self.next_monday)
        self.assertEqual(sunday_view[self.next_week[0]], [('SMS Magic Response- clean', 'needs_file')])
        self.assertEqual(tuesday_view[self.next_week[0]], [])
        self.assertEqual(tuesday_view[tuesday], [('organic text- Clean', 'needs_file')])

    def test_reps_who_were_disabled_or_moved_never_come_back_through_a_template(self):
        self.normal_month()
        _, _, organic = self.template()
        self.erik.is_active = False
        self.erik.save()
        upload = self.upload_planned(organic, xlsx(unique_numbers(4)))
        self.assertEqual(self.manager.get(upload.get_absolute_url()).context['form'].initial['reps'], [self.anthony.pk])
        self.anthony.list_memberships.all().delete()
        self.manager.post(reverse('templates_index'), {'action': 'suggest'})
        drafted = ScheduleTemplate.objects.latest('pk').lists.get(label='organic text- Clean')
        self.assertEqual(list(drafted.reps.all()), [])

    def test_switching_off_or_deleting_a_template_after_uploading_is_harmless(self):
        template, _, organic = self.template()
        upload = self.upload_planned(organic, xlsx(unique_numbers(4)))
        ScheduleTemplate.objects.update(is_active=False)
        self.assertEqual(self.manager.get(upload.get_absolute_url()).status_code, 200)
        self.manager.post(template.get_absolute_url(), {'action': 'delete_template'})
        review = self.manager.get(upload.get_absolute_url())
        self.assertEqual(review.status_code, 200)
        self.assertEqual(review.context['plan_notes'], [])
        self.assertEqual(self.split_via_ui(upload, [self.next_week[1]], [self.anthony]).status_code, 302)

    def test_weekend_lists_are_planned_too(self):
        template, _, _ = self.template()
        self.manager.post(template.get_absolute_url(), {'action': 'add_list', 'new-label': 'Saturday push', 'new-list_type': 'gfs',
                                                        'new-weekdays': [5], 'new-reps': [self.blake.pk]})
        self.assertEqual(self.planned(self.manager, self.next_monday)[self.next_week[5]], [('Saturday push', 'needs_file')])

    def test_thirty_templates_switched_on_keep_the_calendar_fast(self):
        for number in range(30):
            template = ScheduleTemplate.objects.create(name=f'Week {number}', is_active=True, created_by=self.admin)
            for day in (0, 2):
                slot = TemplateList.objects.create(template=template, label=f'List {number}-{day}', list_type='gfs', weekdays=[day])
                slot.reps.set([self.blake, self.emilio])
        with CaptureQueriesContext(connection) as queries:
            response = self.manager.get(reverse('dashboard'), {'date': self.next_monday.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['calendar_days'][0]['planned']), 30)
        self.assertLess(len(queries), 30)

    def test_the_same_list_in_two_switched_on_templates_is_planned_once(self):
        self.normal_month()
        self.draft_and_turn_on('Draft one')
        self.draft_and_turn_on('Draft two (clicked twice)')
        monday = self.planned(self.manager, self.next_monday)[self.next_week[0]]
        self.assertEqual(sorted(monday), [('Donut daily', 'needs_file'), ('SMS Magic Response- clean', 'needs_file')])

    def test_label_typos_week_to_week_are_still_one_routine(self):
        for back, label in [(3, 'organic text- Clean'), (2, 'Organic Text - Clean'), (1, 'ORGANIC TEXT – CLEAN')]:
            week = self.this_week - timedelta(weeks=back)
            self.sent(label, 'ringcentral', [self.anthony], self.week_days(week)[1:5])
        self.manager.post(reverse('templates_index'), {'action': 'suggest'})
        drafted = ScheduleTemplate.objects.get().lists.get()
        self.assertEqual(drafted.note, 'Used in 3 of the last 4 weeks')

    def test_a_list_uploaded_with_a_slightly_different_name_still_fills_the_plan(self):
        self.template()
        upload = self.upload_via_ui(xlsx(unique_numbers(4)), label='organic text - clean')
        self.split_via_ui(upload, self.next_week[1:5], [self.anthony])
        self.assertEqual(self.planned(self.manager, self.next_monday)[self.next_week[2]], [])

    def test_a_second_admin_is_warned_before_uploading_the_same_planned_list(self):
        _, _, organic = self.template()
        self.upload_planned(organic, xlsx(unique_numbers(4)))
        second_admin = get_user_model().objects.create_user('bea.boss', password='x', is_staff=True)
        page = self.signed_in(second_admin).get(reverse('upload_new'), {'slot': organic.pk, 'week': self.next_monday.isoformat()})
        self.assertContains(page, 'already uploaded')


class TemplateMischiefTests(TemplateScenario):
    def setUp(self):
        super().setUp()
        self.standard, self.sms, self.organic = self.template()
        self.next_monday = week_start(self.today) + timedelta(weeks=1)
        self.rep = self.signed_in(self.anthony)

    def test_reps_get_the_same_flat_no_at_every_template_door(self):
        doors = [reverse('templates_index'), self.standard.get_absolute_url(), reverse('template_detail', args=[999999]),
                 reverse('upload_new') + f'?slot={self.organic.pk}&week={self.next_monday}']
        for url in doors:
            for method, data in [('get', {}), ('post', {'action': 'suggest'}), ('post', {'action': 'delete_template'}),
                                 ('post', {'action': 'save_list', 'list_id': self.organic.pk, f'list-{self.organic.pk}-reps': [self.anthony.pk]})]:
                with self.subTest(url=url, method=method, action=data.get('action')):
                    response = getattr(self.rep, method)(url, data)
                    self.assertEqual(response.status_code, 403)
                    self.assertNotContains(response, 'Standard week', status_code=403)
        self.assertEqual(ScheduleTemplate.objects.count(), 1)
        self.assertEqual(TemplateList.objects.count(), 2)

    def test_reps_never_see_the_plan_even_when_they_are_in_it(self):
        for params in ({}, {'week': self.next_monday.isoformat()}, {'date': self.next_monday.isoformat()},
                       {'week': self.next_monday.isoformat(), 'planned': '1', 'show': 'all'}):
            with self.subTest(params=params):
                response = self.rep.get(reverse('dashboard'), params)
                for hidden in ('Planned', 'Needs a file', 'SMS Magic Response- clean', 'organic text- Clean', 'Standard week', '?slot='):
                    self.assertNotContains(response, hidden)

    def test_booby_trapped_links_and_forged_forms_cannot_change_templates(self):
        url = self.standard.get_absolute_url()
        for query in ['?action=delete_template', '?action=update_template&name=Hacked&is_active=',
                      f'?action=remove_list&list_id={self.organic.pk}']:
            self.assertEqual(self.manager.get(url + query).status_code, 200)
        self.assertEqual(self.manager.get(reverse('templates_index') + '?action=suggest').status_code, 200)
        forged = Client(enforce_csrf_checks=True)
        forged.force_login(self.admin)
        for target, data in [(url, {'action': 'delete_template'}), (url, {'action': 'update_template', 'name': 'Hacked'}),
                             (url, {'action': 'remove_list', 'list_id': self.organic.pk}),
                             (reverse('templates_index'), {'action': 'create', 'name': 'Spam'})]:
            self.assertEqual(forged.post(target, data).status_code, 403)
        self.standard.refresh_from_db()
        self.assertEqual((self.standard.name, self.standard.is_active), ('Standard week', True))
        self.assertEqual(ScheduleTemplate.objects.count(), 1)
        self.assertEqual(self.standard.lists.count(), 2)

    def test_tampered_template_forms_are_rejected(self):
        disabled = self.make_rep('gone.rep', 'Gone', 'Rep', 'ringcentral')
        disabled.is_active = False
        disabled.save()
        base = {'new-label': 'Tampered', 'new-list_type': 'ringcentral', 'new-weekdays': [1], 'new-reps': [self.anthony.pk]}
        for name, changes in {
            'day seven': {'new-weekdays': ['7']}, 'day minus one': {'new-weekdays': ['-1']}, 'day as a word': {'new-weekdays': ['Mon']},
            'half a day': {'new-weekdays': ['1.5']}, 'blank day': {'new-weekdays': ['']},
            'made-up group': {'new-list_type': 'admin'}, 'rep that does not exist': {'new-reps': ['999999']},
            'the admin as a rep': {'new-reps': [self.admin.pk]}, 'a disabled rep': {'new-reps': [disabled.pk]},
            'rep as text': {'new-reps': ['abc']}, 'a novel for a name': {'new-label': 'x' * 10000},
        }.items():
            with self.subTest(name):
                response = self.manager.post(self.standard.get_absolute_url(), {'action': 'add_list', **base, **changes})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['new_form'].errors)
        self.assertFalse(TemplateList.objects.filter(label__startswith='Tampered').exists())
        self.manager.post(self.standard.get_absolute_url(), {'action': 'add_list', **base, 'new-weekdays': ['1', '1', '3']})
        self.assertEqual(TemplateList.objects.get(label='Tampered').weekdays, [1, 3])
        too_long = self.manager.post(reverse('templates_index'), {'action': 'create', 'name': 'y' * 10000})
        self.assertTrue(too_long.context['form'].errors)
        for bad in ('abc', '', '-5', '1e3'):
            self.assertEqual(self.manager.post(self.standard.get_absolute_url(), {'action': 'remove_list', 'list_id': bad}).status_code, 404)

    def test_tampered_planned_upload_links_never_link_or_crash(self):
        gone = TemplateList.objects.create(template=ScheduleTemplate.objects.create(name='Doomed', is_active=True, created_by=self.admin),
                                           label='Doomed list', list_type='gfs', weekdays=[0])
        gone_id = gone.pk
        gone.template.delete()
        for slot, week in [(gone_id, self.next_monday.isoformat()), (-1, self.next_monday.isoformat()), (self.organic.pk, '2026-02-30'),
                           (self.organic.pk, '0001-01-01'), (self.organic.pk, 'x' * 5000), (self.organic.pk, '../../etc'),
                           ('1 OR 1=1', self.next_monday.isoformat()), (self.organic.pk, '9999-12-31'), (self.organic.pk, '')]:
            with self.subTest(slot=str(slot)[:12], week=week[:12]):
                page = self.manager.get(reverse('upload_new'), {'slot': slot, 'week': week})
                self.assertEqual(page.status_code, 200)
                self.assertIsNone(page.context['slot'])
                response = self.manager.post(reverse('upload_new'), {'label': 'Probe', 'list_type': 'gfs',
                                                                     'file': xlsx(unique_numbers(1)), 'slot': slot, 'week': week})
                self.assertIsNone(ImportBatch.objects.get(pk=response.url.strip('/').split('/')[-1]).template_list)

    def test_sneaky_characters_in_template_names_are_plain_text(self):
        sneaky = 'Week ‮gnp.exe <b onmouseover=alert(1)>hi</b> ​🦄'
        self.manager.post(self.standard.get_absolute_url(), {'action': 'update_template', 'name': sneaky, 'is_active': 'on'})
        self.manager.post(self.standard.get_absolute_url(), {
            'action': 'add_list', 'new-label': '<svg onload=alert(1)>', 'new-list_type': 'gfs', 'new-weekdays': [2], 'new-reps': []})
        for response in (self.manager.get(reverse('templates_index')), self.manager.get(self.standard.get_absolute_url()),
                         self.manager.get(reverse('dashboard'), {'date': self.next_monday.isoformat()}),
                         self.manager.get(reverse('audit_log'))):
            body = response.content.decode()
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('<b onmouseover', body)
            self.assertNotIn('<svg onload', body)

    def test_skip_cannot_be_abused(self):
        wednesday = self.next_monday + timedelta(days=2)
        good = {'slot': self.organic.pk, 'day': wednesday.isoformat()}
        self.assertEqual(self.rep.post(reverse('plan_skip'), good).status_code, 403)
        self.assertEqual(self.manager.get(reverse('plan_skip'), good).status_code, 405)
        forged = Client(enforce_csrf_checks=True)
        forged.force_login(self.admin)
        self.assertEqual(forged.post(reverse('plan_skip'), good).status_code, 403)
        off, off_slot, _ = self.template(name='Off', active=False)
        for data in [{'slot': self.organic.pk, 'day': self.next_monday.isoformat()},  # organic doesn't run Mondays
                     {'slot': self.organic.pk, 'day': (self.today - timedelta(days=7)).isoformat()},
                     {'slot': self.organic.pk, 'day': 'someday'}, {'slot': self.organic.pk, 'day': '9999-12-31'},
                     {'slot': off_slot.pk, 'day': self.next_monday.isoformat()}, {'slot': 999999, 'day': wednesday.isoformat()},
                     {'slot': 'x', 'day': ''}, {}]:
            with self.subTest(data=data):
                self.assertEqual(self.manager.post(reverse('plan_skip'), data).status_code, 302)
        self.assertFalse(PlanSkip.objects.exists())
        for _ in range(3):
            self.manager.post(reverse('plan_skip'), good)
        self.assertEqual(PlanSkip.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='plan.skipped').count(), 1)
        skip = PlanSkip.objects.get()
        self.assertEqual(self.rep.post(reverse('plan_unskip', args=[skip.pk])).status_code, 403)
        self.assertEqual(self.manager.post(reverse('plan_unskip', args=[999999])).status_code, 404)
        self.assertTrue(PlanSkip.objects.exists())
        rep_week = self.rep.get(reverse('dashboard'), {'week': self.next_monday.isoformat()})
        for hidden in ('Skipped this week', 'Skip this day', reverse('plan_skip')):
            self.assertNotContains(rep_week, hidden)

    def test_history_shows_template_activity_even_with_odd_details(self):
        for action in ('template.suggested', 'template.updated', 'template.list_saved', 'upload.planned', 'template.deleted',
                       'plan.skipped', 'plan.unskipped'):
            AuditEvent.objects.create(actor=self.admin, action=action, target_type='scheduletemplate', target_id='999999',
                                      details={'list_count': 'lots', 'is_active': 'maybe', 'weekdays': {'x': 1}, 'day': 'Caturday'})
        response = self.manager.get(reverse('audit_log'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Template drafted from past weeks')
