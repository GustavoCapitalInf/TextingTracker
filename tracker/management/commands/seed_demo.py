"""Populate an explicitly enabled, empty development database with synthetic data."""

from datetime import timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from openpyxl import Workbook

from tracker.models import ImportBatch, PhoneNumber, RepListMembership, TextingListType
from tracker.services import (
    _phone_transaction, import_workbook, publish_batches,
)


def _spreadsheet(numbers, filename):
    workbook = Workbook()
    workbook.active.title = "DEMO phone numbers"
    workbook.active.append(["Phone number"])
    for number in numbers:
        workbook.active.append([number])
    content = BytesIO()
    workbook.save(content)
    workbook.close()
    return SimpleUploadedFile(
        filename, content.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _number(index):
    # The North American 555-0100–0199 range is reserved for fictional use.
    return f"212555{100 + index:04d}"


class Command(BaseCommand):
    help = "Create an empty development demo using fictional phone numbers and generated credentials."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm", action="store_true", help="Confirm this is a separate, empty demo database."
        )

    def handle(self, *args, **options):
        if not settings.DEBUG or not getattr(settings, "DEMO_MODE", False):
            raise CommandError("Demo seeding requires development mode and PHONETRACKER_DEMO=1.")
        if not options["confirm"]:
            raise CommandError("Pass --confirm to seed a separate, empty development demo database.")
        credentials_path = Path(settings.DATA_DIR).resolve() / "demo-credentials.json"
        if credentials_path.exists():
            raise CommandError(f"Demo credentials already exist: {credentials_path}")
        created_credentials = False
        try:
            with _phone_transaction():
                User = get_user_model()
                if User.objects.exists() or PhoneNumber.objects.exists() or ImportBatch.objects.exists():
                    raise CommandError("Demo seeding requires an empty database with no users, phones, or uploads.")
                manager_password = secrets.token_urlsafe(24)
                manager = User.objects.create_user(
                    "demo.manager", password=manager_password, is_staff=True,
                    first_name="Manager", last_name="(demo)",
                )
                credentials = {
                    "manager": {"username": manager.username, "password": manager_password},
                    "reps": [],
                }
                reps = []
                for username, first, last in [
                    ("emilio", "Emilio", "Arguello"),
                    ("anthony", "Anthony", "Diaz"),
                    ("erik", "Erik", "Anderson"),
                    ("michael", "Michael", "Cifuentes"),
                    ("kip", "Kip", "Langat"),
                ]:
                    password = secrets.token_urlsafe(24)
                    rep = User.objects.create_user(
                        f"demo.{username}", password=password,
                        first_name=first, last_name=f"{last} (demo)",
                    )
                    reps.append(rep)
                    RepListMembership.objects.create(rep=rep, list_type=TextingListType.RINGCENTRAL)
                    credentials["reps"].append({"username": rep.username, "password": password})

                today = timezone.localdate()
                daily = import_workbook(
                    _spreadsheet([_number(index) for index in range(40)], "DEMO-today.xlsx"),
                    manager, "DEMO · Today's leads",
                    list_type=TextingListType.RINGCENTRAL,
                )
                publish_batches(daily, [today], reps, manager)

                upcoming = import_workbook(
                    _spreadsheet([_number(index) for index in range(40, 85)], "DEMO-upcoming.xlsx"),
                    manager, "DEMO · Four-day lead schedule",
                    list_type=TextingListType.RINGCENTRAL,
                )
                dates = [today + timedelta(days=offset) for offset in range(1, 5)]
                publish_batches(upcoming, dates, reps, manager)
                review_values = [
                    _number(0), _number(1), _number(20),
                    *[_number(index) for index in range(85, 90)],
                    _number(85), "not a phone number",
                ]
                import_workbook(
                    _spreadsheet(review_values, "DEMO-review.xlsx"),
                    manager, "DEMO · Import review with warnings",
                    list_type=TextingListType.RINGCENTRAL,
                )

                credentials_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(
                    credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                )
                created_credentials = True
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(credentials, handle, indent=2)
                    handle.write("\n")
        except Exception:
            if created_credentials:
                credentials_path.unlink(missing_ok=True)
            raise
        self.stdout.write(self.style.SUCCESS(f"Demo ready. Local credentials: {credentials_path}"))
