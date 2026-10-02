"""Split and assign: picking list dates with Mon–Sun day buttons for two weeks, plus other dates."""
import re
from datetime import timedelta

from django.urls import reverse

from tracker.models import RepBatch, ScheduleTemplate, TemplateList
from tracker.tests.test_real_world import Scenario, fake_numbers, xlsx

BUTTON = re.compile(r'name="days" value="(\d{4}-\d{2}-\d{2})" aria-label="([^"]*)"( checked)?( disabled)?>')


class SplitDayButtonTests(Scenario):
    def setUp(self):
        super().setUp()
        self.monday = self.today - timedelta(days=self.today.weekday())
        self.upload = self.upload_via_ui(xlsx(fake_numbers(12)), label='Day buttons', list_type='ringcentral')

    def buttons(self, upload=None):
        return BUTTON.findall(self.manager.get((upload or self.upload).get_absolute_url()).content.decode())

    def split(self, reps, **data):
        return self.manager.post(reverse('upload_publish', args=[self.upload.pk]), {
            'rep_count': len(reps), 'reps': [rep.pk for rep in reps], 'intent': 'create', **data})

    def scheduled(self):
        return sorted(set(RepBatch.objects.filter(upload=self.upload).values_list('scheduled_date', flat=True)))

    def test_two_weeks_of_mon_to_sun_with_today_ticked_and_past_days_locked(self):
        buttons = self.buttons()
        self.assertEqual([value for value, *_ in buttons], [(self.monday + timedelta(days=n)).isoformat() for n in range(14)])
        self.assertEqual([value for value, _, checked, _ in buttons if checked], [self.today.isoformat()])
        self.assertEqual([value for value, _, _, disabled in buttons if disabled],
                         [(self.monday + timedelta(days=n)).isoformat() for n in range(self.today.weekday())])
        self.assertTrue(buttons[0][1].startswith('Monday, ') and buttons[6][1].startswith('Sunday, '))
        page = self.manager.get(self.upload.get_absolute_url())
        self.assertContains(page, '<strong>This week</strong>')
        self.assertContains(page, '<strong>Next week</strong>')
        self.assertNotContains(page, 'data-date-preset')

    def test_ticked_days_become_the_list_dates(self):
        next_monday = self.monday + timedelta(weeks=1)
        response = self.split([self.anthony, self.erik], days=[self.today.isoformat(), next_monday.isoformat()])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.scheduled(), [self.today, next_monday])

    def test_other_dates_add_to_the_ticked_days(self):
        later = self.monday + timedelta(weeks=3, days=2)
        self.split([self.anthony], days=[self.today.isoformat()], other_dates=f'{later.isoformat()}\n{self.today.isoformat()}')
        self.assertEqual(self.scheduled(), [self.today, later])

    def test_mistakes_are_explained_and_nothing_is_created(self):
        cases = [({}, 'Pick at least one day.'),
                 ({'days': [(self.today - timedelta(days=1)).isoformat()]}, 'List dates must be today or later.'),
                 ({'days': [self.today.isoformat()], 'other_dates': 'next tuesday'}, 'Use YYYY-MM-DD for every date.'),
                 ({'days': ['2026-13-45']}, 'Pick at least one day.')]
        for data, message in cases:
            with self.subTest(data=data):
                response = self.split([self.anthony], **data)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, message)
        self.assertEqual(self.scheduled(), [])

    def test_a_rejected_split_keeps_the_ticked_days(self):
        next_tuesday = self.monday + timedelta(weeks=1, days=1)
        response = self.split([self.anthony, self.erik], days=[next_tuesday.isoformat()], rep_count=1)
        ticked = [value for value, _, checked, _ in BUTTON.findall(response.content.decode()) if checked]
        self.assertEqual(ticked, [next_tuesday.isoformat()])

    def test_a_planned_list_ticks_its_days_in_its_own_week(self):
        week = self.monday + timedelta(weeks=3)
        template = ScheduleTemplate.objects.create(name='Week', is_active=True, created_by=self.admin)
        slot = TemplateList.objects.create(template=template, label='Organic', list_type='ringcentral', weekdays=[1, 2, 3])
        slot.reps.set([self.anthony])
        response = self.manager.post(reverse('upload_new'), {'label': 'Organic', 'list_type': 'ringcentral', 'file': xlsx(fake_numbers(6, '646')),
                                                             'slot': slot.pk, 'week': week.isoformat()})
        upload = self.upload.__class__.objects.get(pk=response.url.strip('/').split('/')[-1])
        buttons = self.buttons(upload)
        self.assertEqual(buttons[0][0], week.isoformat())
        self.assertEqual([value for value, _, checked, _ in buttons if checked], [(week + timedelta(days=n)).isoformat() for n in (1, 2, 3)])
        page = self.manager.get(upload.get_absolute_url())
        self.assertContains(page, f'<strong>Week of {week:%b} {week.day}</strong>')
        self.assertNotContains(page, '<details class="other-dates" open>')
