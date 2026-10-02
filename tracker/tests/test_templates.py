"""Weekly templates: learning from past weeks, editing, and planning the calendar."""
from datetime import datetime, time, timedelta
from itertools import count

from django.urls import reverse
from django.utils import timezone

from tracker.models import ImportBatch, RepBatch, ScheduleTemplate, TemplateList
from tracker.planning import week_start
from tracker.services import close_upload, import_workbook, publish_batches
from tracker.tests.test_real_world import Scenario, company_time, xlsx


_numbers = count(2000)


def unique_numbers(how_many):
    return [f'(212) 555-{next(_numbers):04d}' for _ in range(how_many)]


class TemplateScenario(Scenario):
    def sent(self, label, list_type, reps, days):
        """A published list whose batches are dated `days`, which may be in the past."""
        upload = import_workbook(xlsx(unique_numbers(len(days) * len(reps))), self.admin, label, list_type=list_type)
        placeholders = [self.today + timedelta(days=offset) for offset in range(len(days))]
        publish_batches(upload, placeholders, reps, self.admin)
        for placeholder, day in zip(placeholders, days):
            RepBatch.objects.filter(upload=upload, scheduled_date=placeholder).update(scheduled_date=day)
        return upload

    def template(self, name='Standard week', active=True):
        template = ScheduleTemplate.objects.create(name=name, is_active=active, created_by=self.admin)
        sms = TemplateList.objects.create(template=template, label='SMS Magic Response- clean', list_type='ringcentral', weekdays=[0])
        sms.reps.set([self.anthony])
        organic = TemplateList.objects.create(template=template, label='organic text- Clean', list_type='ringcentral', weekdays=[1, 2, 3, 4])
        organic.reps.set([self.anthony, self.erik])
        return template, sms, organic

    def week_days(self, monday):
        return [monday + timedelta(days=offset) for offset in range(7)]

    def planned(self, client, day):
        calendar = client.get(reverse('dashboard'), {'date': day.isoformat()}).context['calendar_days']
        return {entry['date']: [(plan['slot'].label, plan['state']) for plan in entry.get('planned', [])] for entry in calendar}


class TemplateLearningTests(TemplateScenario):
    def test_drafting_from_past_weeks_finds_the_repeating_lists(self):
        this_week = week_start(self.today)
        weeks = [this_week - timedelta(weeks=back) for back in (3, 2, 1)]
        for week in weeks:
            reps = [self.anthony, self.erik, self.emilio] if week == weeks[-1] else [self.anthony, self.erik]
            self.sent('SMS Magic Response- clean', 'ringcentral', reps, [week])
        self.sent('Organic text-  CLEAN', 'ringcentral', [self.anthony, self.erik], self.week_days(weeks[1])[1:5])
        self.sent('organic text- Clean', 'ringcentral', [self.anthony, self.erik], self.week_days(weeks[2])[1:5])
        self.sent('Holiday blast', 'gfs', [self.blake], [weeks[0] + timedelta(days=2)])
        close_upload(self.sent('Cancelled list', 'gfs', [self.blake], [weeks[2] + timedelta(days=3)]), self.admin)
        self.sent('Too old', 'gfs', [self.blake], [this_week - timedelta(weeks=6)])
        import_workbook(xlsx(unique_numbers(2)), self.admin, 'Never sent', list_type='gfs')

        response = self.manager.post(reverse('templates_index'), {'action': 'suggest'})
        template = ScheduleTemplate.objects.get()
        self.assertRedirects(response, template.get_absolute_url())
        self.assertFalse(template.is_active)
        drafted = [(slot.label, slot.list_type, slot.weekdays, slot.note, {rep.pk for rep in slot.reps.all()})
                   for slot in template.lists.all()]
        self.assertEqual(drafted, [
            ('SMS Magic Response- clean', 'ringcentral', [0], 'Used in 3 of the last 4 weeks',
             {self.anthony.pk, self.erik.pk, self.emilio.pk}),
            ('organic text- Clean', 'ringcentral', [1, 2, 3, 4], 'Used in 2 of the last 4 weeks', {self.anthony.pk, self.erik.pk}),
            ('Holiday blast', 'gfs', [2], 'Used in 1 of the last 4 weeks', {self.blake.pk}),
        ])
        self.assertContains(self.manager.get(reverse('audit_log')), 'Template drafted from past weeks')

    def test_nothing_to_learn_from_yet(self):
        response = self.manager.post(reverse('templates_index'), {'action': 'suggest'}, follow=True)
        self.assertContains(response, 'no lists from the last 4 weeks')
        self.assertFalse(ScheduleTemplate.objects.exists())

    def test_reps_who_left_the_group_are_not_suggested(self):
        self.sent('organic text- Clean', 'ringcentral', [self.anthony, self.erik], [week_start(self.today) - timedelta(days=6)])
        self.erik.list_memberships.all().delete()
        self.manager.post(reverse('templates_index'), {'action': 'suggest'})
        self.assertEqual(list(TemplateList.objects.get().reps.all()), [self.anthony])


class TemplateEditingTests(TemplateScenario):
    def list_data(self, prefix='new', **values):
        data = {'label': 'organic text- Clean', 'list_type': 'ringcentral', 'weekdays': [1, 2, 3, 4],
                'reps': [self.anthony.pk, self.erik.pk], **values}
        return {f'{prefix}-{key}': value for key, value in data.items()}

    def test_admin_builds_a_template_by_hand(self):
        response = self.manager.post(reverse('templates_index'), {'action': 'create', 'name': 'Clean week'})
        template = ScheduleTemplate.objects.get(name='Clean week')
        self.assertRedirects(response, template.get_absolute_url())
        self.manager.post(template.get_absolute_url(), {'action': 'add_list', **self.list_data()})
        slot = template.lists.get()
        self.assertEqual((slot.weekdays, slot.weekday_label), ([1, 2, 3, 4], 'Tue–Fri'))
        self.manager.post(template.get_absolute_url(), {
            'action': 'save_list', 'list_id': slot.pk,
            **self.list_data(f'list-{slot.pk}', label='SMS Magic Response- clean', weekdays=[0, 2], reps=[self.anthony.pk])})
        slot.refresh_from_db()
        self.assertEqual((slot.label, slot.weekday_label, list(slot.reps.all())), ('SMS Magic Response- clean', 'Mon, Wed', [self.anthony]))
        self.manager.post(template.get_absolute_url(), {'action': 'update_template', 'name': 'Clean week', 'is_active': 'on'})
        template.refresh_from_db()
        self.assertTrue(template.is_active)
        self.manager.post(template.get_absolute_url(), {'action': 'remove_list', 'list_id': slot.pk})
        self.assertFalse(template.lists.exists())

    def test_list_rules_are_enforced(self):
        template, _, _ = self.template(active=False)
        before = TemplateList.objects.count()
        for name, values in {
            'rep outside the group': {'reps': [self.blake.pk]},
            'no days': {'weekdays': []},
            'no name': {'label': '  '},
            'made-up day': {'weekdays': [9]},
            'made-up group': {'list_type': 'carrier-pigeon'},
        }.items():
            with self.subTest(name):
                response = self.manager.post(template.get_absolute_url(), {'action': 'add_list', **self.list_data(**values)})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['new_form'].errors)
        self.assertEqual(TemplateList.objects.count(), before)
        other, _, _ = self.template(name='Other', active=False)
        foreign = other.lists.first()
        self.assertEqual(self.manager.post(template.get_absolute_url(), {'action': 'save_list', 'list_id': foreign.pk}).status_code, 404)

    def test_rep_checkboxes_say_which_group_each_rep_is_in(self):
        template, _, _ = self.template(active=False)
        page = self.manager.get(template.get_absolute_url())
        body = page.content.decode()
        self.assertRegex(body, rf'name="new-reps" value="{self.anthony.pk}"[^>]*data-groups="ringcentral"')
        self.assertRegex(body, rf'name="new-reps" value="{self.blake.pk}"[^>]*data-groups="gfs"')
        self.assertRegex(body, rf'name="new-reps" value="{self.emilio.pk}"[^>]*data-groups="gfs ringcentral"')
        self.assertNotContains(page, 'Anthony Diaz · Ringcentral')
        self.assertContains(page, 'data-rep-filter')

    def test_reps_cannot_see_or_change_templates(self):
        template, slot, _ = self.template()
        rep = self.signed_in(self.anthony)
        for method, url, data in [('get', reverse('templates_index'), {}), ('post', reverse('templates_index'), {'action': 'suggest'}),
                                  ('get', template.get_absolute_url(), {}),
                                  ('post', template.get_absolute_url(), {'action': 'delete_template'}),
                                  ('post', template.get_absolute_url(), {'action': 'remove_list', 'list_id': slot.pk})]:
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(rep, method)(url, data).status_code, 403)
        self.assertEqual(TemplateList.objects.filter(template=template).count(), 2)
        self.assertNotContains(rep.get(reverse('dashboard')), reverse('templates_index'))

    def test_names_are_shown_as_plain_text(self):
        template, slot, _ = self.template(name='<script>alert("t")</script>')
        TemplateList.objects.filter(pk=slot.pk).update(label='<img src=x onerror=alert(1)>')
        next_monday = week_start(self.today) + timedelta(weeks=1)
        for response in (self.manager.get(reverse('templates_index')), self.manager.get(template.get_absolute_url()),
                         self.manager.get(reverse('dashboard'), {'date': next_monday.isoformat()})):
            self.assertNotIn('<script>alert("t")', response.content.decode())
            self.assertNotIn('<img src=x onerror', response.content.decode())

    def test_deleting_a_template_keeps_lists_created_from_it(self):
        template, _, organic = self.template()
        upload = import_workbook(xlsx(unique_numbers(2)), self.admin, 'organic text- Clean', list_type='ringcentral')
        ImportBatch.objects.filter(pk=upload.pk).update(template_list=organic, planned_week=week_start(self.today))
        self.manager.post(template.get_absolute_url(), {'action': 'delete_template'})
        self.assertFalse(ScheduleTemplate.objects.exists())
        upload.refresh_from_db()
        self.assertIsNone(upload.template_list)
        self.assertEqual(upload.rows.count(), 2)


class TemplatePlanningTests(TemplateScenario):
    def setUp(self):
        super().setUp()
        self.standard, self.sms, self.organic = self.template()
        self.monday = week_start(self.today) + timedelta(weeks=1)
        self.days = self.week_days(self.monday)

    def test_calendar_shows_planned_lists_that_need_a_file(self):
        planned = self.planned(self.manager, self.monday)
        self.assertEqual(planned[self.days[0]], [('SMS Magic Response- clean', 'needs_file')])
        for day in self.days[1:5]:
            self.assertEqual(planned[day], [('organic text- Clean', 'needs_file')])
        self.assertEqual(planned[self.days[5]], [])
        page = self.manager.get(reverse('dashboard'), {'date': self.monday.isoformat()})
        self.assertContains(page, f'?slot={self.organic.pk}&amp;week={self.monday.isoformat()}')
        rep_week = self.signed_in(self.anthony).get(reverse('dashboard'), {'week': self.monday.isoformat()})
        self.assertNotContains(rep_week, 'organic text- Clean')
        self.assertNotContains(rep_week, 'Planned')

    def test_templates_that_are_off_and_days_that_passed_are_not_planned(self):
        wednesday = self.days[2]
        with company_time(datetime.combine(wednesday, time(10))):
            client = self.signed_in(self.admin)
            planned = self.planned(client, wednesday)
        self.assertEqual(planned[self.days[0]], [])
        self.assertEqual(planned[self.days[1]], [])
        self.assertEqual(planned[wednesday], [('organic text- Clean', 'needs_file')])
        ScheduleTemplate.objects.update(is_active=False)
        self.assertFalse(any(self.planned(self.manager, self.monday).values()))

    def test_uploading_for_a_planned_list_fills_in_the_split(self):
        link = {'slot': self.organic.pk, 'week': self.monday.isoformat()}
        form_page = self.manager.get(reverse('upload_new'), link)
        self.assertEqual(form_page.context['form'].initial, {'label': 'organic text- Clean', 'list_type': 'ringcentral'})
        self.assertContains(form_page, 'Planned list: organic text- Clean')
        response = self.manager.post(reverse('upload_new'), {
            'label': 'organic text- Clean', 'list_type': 'ringcentral', 'file': xlsx(unique_numbers(8)), **link})
        upload = ImportBatch.objects.get(pk=response.url.strip('/').split('/')[-1])
        self.assertEqual((upload.template_list, upload.planned_week), (self.organic, self.monday))

        review = self.manager.get(upload.get_absolute_url())
        initial = review.context['form'].initial
        self.assertEqual(initial['dates'].split('\n'), [day.isoformat() for day in self.days[1:5]])
        self.assertEqual((initial['rep_count'], sorted(initial['reps'])), (2, sorted([self.anthony.pk, self.erik.pk])))
        self.assertContains(review, 'Days and reps were picked from your “Standard week” template')
        self.assertEqual(self.planned(self.manager, self.monday)[self.days[1]], [('organic text- Clean', 'uploaded')])

        self.split_via_ui(upload, self.days[1:5], [self.anthony, self.erik])
        upload.refresh_from_db()
        self.assertEqual(upload.status, 'published')
        planned = self.planned(self.manager, self.monday)
        self.assertEqual(planned[self.days[1]], [])
        self.assertEqual(planned[self.days[0]], [('SMS Magic Response- clean', 'needs_file')])

    def test_prefill_leaves_out_reps_who_left_and_days_that_passed(self):
        self.erik.list_memberships.all().delete()
        upload = import_workbook(xlsx(unique_numbers(4)), self.admin, 'organic text- Clean', list_type='ringcentral')
        ImportBatch.objects.filter(pk=upload.pk).update(template_list=self.organic, planned_week=self.monday)
        with company_time(datetime.combine(self.days[3], time(8))):
            review = self.signed_in(self.admin).get(upload.get_absolute_url())
        initial = review.context['form'].initial
        self.assertEqual(initial['dates'].split('\n'), [self.days[3].isoformat(), self.days[4].isoformat()])
        self.assertEqual(initial['reps'], [self.anthony.pk])
        self.assertContains(review, '1 planned rep isn’t an active member of Ringcentral Texting (Clean) and was left out.')
        self.assertContains(review, 'Planned days that have already passed were left out.')

    def test_a_list_uploaded_without_the_link_still_fills_its_plan(self):
        upload = self.upload_via_ui(xlsx(unique_numbers(4)), label='Organic Text- clean')
        self.split_via_ui(upload, self.days[1:5], [self.anthony, self.erik])
        planned = self.planned(self.manager, self.monday)
        self.assertEqual(planned[self.days[2]], [])
        self.assertEqual(planned[self.days[0]], [('SMS Magic Response- clean', 'needs_file')])

    def test_bad_plan_links_are_ignored(self):
        off, off_slot, _ = self.template(name='Off', active=False)
        last_week = (self.monday - timedelta(weeks=2)).isoformat()
        for link in [{'slot': 999999, 'week': self.monday.isoformat()}, {'slot': 'abc', 'week': 'tomorrow'},
                     {'slot': off_slot.pk, 'week': self.monday.isoformat()}, {'slot': self.organic.pk, 'week': last_week},
                     {'slot': self.organic.pk, 'week': '9999-12-31'}]:
            with self.subTest(link=link):
                page = self.manager.get(reverse('upload_new'), link)
                self.assertEqual(page.status_code, 200)
                self.assertIsNone(page.context['slot'])
        response = self.manager.post(reverse('upload_new'), {
            'label': 'Unplanned', 'list_type': 'gfs', 'file': xlsx(unique_numbers(1)), 'slot': off_slot.pk, 'week': self.monday.isoformat()})
        upload = ImportBatch.objects.get(pk=response.url.strip('/').split('/')[-1])
        self.assertIsNone(upload.template_list)
