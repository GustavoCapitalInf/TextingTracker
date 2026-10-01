from datetime import date
import re

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import TextingListType
from .services import eligible_reps


class ListTypeForm(forms.Form):
    list_type = forms.ChoiceField(label='List type', choices=[('', 'Choose a list type'), *TextingListType.choices])


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
        if list_type in TextingListType.values:
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

