from datetime import date, timedelta
import re

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import WEEKDAY_NAMES
from . import texting_lists
from .services import eligible_reps


class ListTypeForm(forms.Form):
    list_type = forms.ChoiceField(label='Texting list', choices=lambda: [('', 'Choose a texting list'), *texting_lists.active_choices()])


class UploadForm(ListTypeForm):
    label = forms.CharField(max_length=120, label='List name', widget=forms.TextInput(attrs={'placeholder': 'e.g. Organic leads · October 5–9'}))
    file = forms.FileField(label='Excel spreadsheet', widget=forms.FileInput(attrs={'accept': '.xlsx'}))


class RepChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return obj.get_full_name() or obj.username


def _parse_date_lines(raw):
    """Dates typed one per line (or comma-separated) as YYYY-MM-DD; [] when blank."""
    parts = [s.strip() for s in re.split(r'[\n,]+', raw or '') if s.strip()]
    if len(parts) > 31:
        raise ValidationError('Choose between 1 and 31 list dates.')
    try:
        if any(not re.fullmatch(r'\d{4}-\d{2}-\d{2}', part) for part in parts):
            raise ValueError
        dates = [date.fromisoformat(part) for part in parts]
    except ValueError:
        raise ValidationError('Use YYYY-MM-DD for every date.')
    if len(set(dates)) != len(dates):
        raise ValidationError('Each list date must appear once.')
    return dates


class DayChoices(forms.MultipleChoiceField):
    """Ticked day buttons, each a YYYY-MM-DD value. Any well-formed date is accepted; clean() checks the rest."""
    widget = forms.CheckboxSelectMultiple

    def valid_value(self, value):
        return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value)))


class PublishForm(forms.Form):
    days = DayChoices(required=False, label='List dates')
    other_dates = forms.CharField(required=False, label='Other dates',
                                  help_text='Dates outside these two weeks, one per line as YYYY-MM-DD.',
                                  widget=forms.Textarea(attrs={'rows': 3, 'placeholder': '2026-10-19'}))
    # Older pages, scripts and tests post every date in one `dates` field; it is still accepted, never rendered.
    dates = forms.CharField(required=False, widget=forms.Textarea)
    rep_count = forms.IntegerField(min_value=1, max_value=100, label='Split into how many people?', initial=1)
    reps = RepChoices(queryset=get_user_model().objects.none(), label='Assign to reps', widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        list_type = kwargs.pop('list_type', None)
        super().__init__(*args, **kwargs)
        if list_type and texting_lists.get(list_type):
            self.fields['reps'].queryset = eligible_reps(list_type).order_by('first_name', 'last_name', 'username')
        self.today = timezone.localdate()
        if self.is_bound:
            chosen = self._dates_from(self.data.getlist('days') if hasattr(self.data, 'getlist') else self.data.get('days') or [])
        else:
            # A planned upload pre-fills its days; otherwise today is ticked.
            try:
                chosen = _parse_date_lines(self.initial.get('dates')) or [self.today]
            except ValidationError:
                chosen = [self.today]
        future = [day for day in chosen if day >= self.today]
        self.week_one = _week_start(min(future) if future else self.today)
        self.window = [self.week_one + timedelta(days=offset) for offset in range(14)]
        if not self.is_bound:
            self.initial = {**self.initial,
                            'days': [day.isoformat() for day in chosen if day in self.window],
                            'other_dates': '\n'.join(day.isoformat() for day in chosen if day not in self.window)}

    @staticmethod
    def _dates_from(values):
        result = []
        for value in values:
            try:
                result.append(date.fromisoformat(value))
            except (TypeError, ValueError):
                continue
        return result

    def day_weeks(self):
        """Two rows of Mon–Sun buttons for the template, with what each button shows."""
        picked = set(self['days'].value() or [])
        rows = []
        for index in (0, 1):
            first = self.week_one + timedelta(weeks=index)
            this_week = _week_start(self.today)
            title = 'This week' if first == this_week else 'Next week' if first == this_week + timedelta(weeks=1) else f'Week of {first:%b} {first.day}'
            last = first + timedelta(days=6)
            span = f'{first:%b} {first.day} – {last.day}' if first.month == last.month else f'{first:%b} {first.day} – {last:%b} {last.day}'
            days = [{'iso': day.isoformat(), 'name': f'{day:%a}', 'number': day.day, 'month': f'{day:%b}',
                     'label': f'{day:%A}, {day:%B} {day.day}', 'checked': day.isoformat() in picked,
                     'past': day < self.today, 'today': day == self.today}
                    for day in (first + timedelta(days=offset) for offset in range(7))]
            rows.append({'title': title, 'span': span, 'days': days})
        return rows

    def clean(self):
        data = super().clean()
        chosen, problems = set(self._dates_from(data.get('days') or [])), []
        for name in ('other_dates', 'dates'):
            try:
                chosen.update(_parse_date_lines(data.get(name)))
            except ValidationError as error:
                self.add_error(name if name == 'other_dates' else 'days', error)
                problems.append(name)
        if not problems:
            if not chosen:
                self.add_error('days', 'Pick at least one day.')
            elif len(chosen) > 31:
                self.add_error('days', 'Choose between 1 and 31 list dates.')
            elif min(chosen) < self.today:
                self.add_error('days', 'List dates must be today or later.')
            else:
                data['dates'] = sorted(chosen)
        if 'reps' in data and 'rep_count' in data and len(data['reps']) != data['rep_count']:
            self.add_error('reps', f"Select exactly {data['rep_count']} reps to match the split.")
        return data


def _week_start(day):
    return day - timedelta(days=day.weekday())


class TemplateForm(forms.Form):
    name = forms.CharField(max_length=120, label='Plan name')
    is_active = forms.BooleanField(required=False, label='Turn on: show these lists as reminders on the calendar')


class RepWithGroups(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return obj.get_full_name() or obj.username


class RepCheckboxes(forms.CheckboxSelectMultiple):
    """Each checkbox carries its rep's texting groups so the page can list only the chosen group."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        rep = getattr(value, 'instance', None)
        if rep is not None:
            option['attrs']['data-groups'] = ' '.join(m.list_type for m in rep.list_memberships.all())
        return option


class TemplateListForm(forms.Form):
    label = forms.CharField(max_length=120, label='List name', widget=forms.TextInput(attrs={'placeholder': 'e.g. organic text- Clean'}))
    list_type = forms.ChoiceField(label='Texting list', choices=lambda: [('', 'Choose a texting list'), *texting_lists.active_choices()])
    weekdays = forms.TypedMultipleChoiceField(
        label='Days', coerce=int, choices=list(enumerate(WEEKDAY_NAMES)), widget=forms.CheckboxSelectMultiple,
        error_messages={'required': 'Choose at least one day.'},
    )
    reps = RepWithGroups(
        queryset=get_user_model().objects.none(), required=False, label='Reps', widget=RepCheckboxes,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['reps'].queryset = get_user_model().objects.filter(
            is_active=True, is_staff=False, is_superuser=False, list_memberships__isnull=False,
        ).distinct().prefetch_related('list_memberships').order_by('first_name', 'last_name', 'username')

    def clean(self):
        data = super().clean()
        list_type, reps = data.get('list_type'), data.get('reps')
        if list_type and reps:
            outside = [rep for rep in reps if not any(m.list_type == list_type for m in rep.list_memberships.all())]
            if outside:
                names = ', '.join(rep.get_full_name() or rep.username for rep in outside)
                self.add_error('reps', f"{names} {'is' if len(outside) == 1 else 'are'} not in {texting_lists.label(list_type)}.")
        return data


class TextingListForm(forms.Form):
    name = forms.CharField(max_length=80, label='List name', widget=forms.TextInput(attrs={'placeholder': 'e.g. SMS Magic'}))
    nickname = forms.CharField(max_length=40, required=False, label='Nickname (optional)',
                               help_text='A short tag shown next to the name, like “Clean” or “Donut”.')


class TextingListSettingsForm(TextingListForm):
    is_active = forms.BooleanField(required=False, label='Show this list for new uploads and weekly plans')


class TextingListRepsForm(forms.Form):
    reps = RepWithGroups(queryset=get_user_model().objects.none(), required=False, label='Reps on this list',
                         widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['reps'].queryset = texting_lists.rep_queryset().order_by('first_name', 'last_name', 'username')


class TextingListCreateForm(TextingListForm, TextingListRepsForm):
    field_order = ['name', 'nickname', 'reps']


class TextTemplateForm(forms.Form):
    name = forms.CharField(max_length=80, label='Template name',
                           widget=forms.TextInput(attrs={'placeholder': 'e.g. Follow-up after no reply'}))
    body = forms.CharField(max_length=1600, label='Text message', strip=False,
                           widget=forms.Textarea(attrs={'rows': 6, 'placeholder': 'The exact text reps should send.'}),
                           help_text='Reps see this exactly as written, line breaks included, and can copy it with one tap.')
