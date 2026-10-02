from datetime import date
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


class PublishForm(forms.Form):
    dates = forms.CharField(label='List dates', help_text='One date per line, using YYYY-MM-DD.', widget=forms.Textarea(attrs={'rows': 4, 'placeholder': '2026-10-05'}))
    rep_count = forms.IntegerField(min_value=1, max_value=100, label='Split into how many people?', initial=1)
    reps = RepChoices(queryset=get_user_model().objects.none(), label='Assign to reps', widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        list_type = kwargs.pop('list_type', None)
        super().__init__(*args, **kwargs)
        if list_type and texting_lists.get(list_type):
            self.fields['reps'].queryset = eligible_reps(list_type).order_by('first_name', 'last_name', 'username')
        if not self.is_bound:
            self.fields['dates'].initial = timezone.localdate().isoformat()

    def clean_dates(self):
        raw = self.cleaned_data['dates']
        parts = [s.strip() for s in re.split(r'[\n,]+', raw) if s.strip()]
        if not 1 <= len(parts) <= 31:
            raise ValidationError('Choose between 1 and 31 list dates.')
        try:
            if any(not re.fullmatch(r'\d{4}-\d{2}-\d{2}', part) for part in parts):
                raise ValueError
            dates = [date.fromisoformat(part) for part in parts]
        except ValueError:
            raise ValidationError('Use YYYY-MM-DD for every date.')
        if len(set(dates)) != len(dates):
            raise ValidationError('Each list date must appear once.')
        if min(dates) < timezone.localdate():
            raise ValidationError('List dates must be today or later.')
        return sorted(dates)

    def clean(self):
        data = super().clean()
        if 'reps' in data and 'rep_count' in data and len(data['reps']) != data['rep_count']:
            self.add_error('reps', f"Select exactly {data['rep_count']} reps to match the split.")
        return data



class TemplateForm(forms.Form):
    name = forms.CharField(max_length=120, label='Template name')
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
    is_active = forms.BooleanField(required=False, label='Show this list for new uploads and templates')


class TextingListRepsForm(forms.Form):
    reps = RepWithGroups(queryset=get_user_model().objects.none(), required=False, label='Reps on this list',
                         widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['reps'].queryset = texting_lists.rep_queryset().order_by('first_name', 'last_name', 'username')


class TextingListCreateForm(TextingListForm, TextingListRepsForm):
    field_order = ['name', 'nickname', 'reps']
