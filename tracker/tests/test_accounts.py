from io import StringIO
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from tracker.account_forms import RepCreationForm, RepMembershipForm
from tracker.accounts import create_rep, reset_rep_password, update_rep_memberships
from tracker.models import AuditEvent, RepListMembership, TextingListType


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AccountManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = get_user_model().objects.create_user("manager", password="Keep-Admin-Password-123", is_staff=True)
        cls.rep = get_user_model().objects.create_user("existing.rep", first_name="Existing", last_name="Rep")

    def test_created_credentials_are_unique_hashed_and_not_audited(self):
        first, first_password = create_rep(self.manager, "Blake", "Fiorito", "blake.fiorito", [TextingListType.GFS])
        second, second_password = create_rep(self.manager, "Emilio", "Arguello", "emilio.arguello", TextingListType.values)
        self.assertNotEqual(first_password, second_password)
        for rep, password in [(first, first_password), (second, second_password)]:
            self.assertGreaterEqual(len(password), 20)
            self.assertTrue(rep.check_password(password))
            self.assertNotEqual(rep.password, password)
            self.assertTrue(rep.is_active)
            self.assertFalse(rep.is_staff)
            self.assertFalse(rep.is_superuser)
            self.assertNotIn(password, json.dumps(list(AuditEvent.objects.values("action", "details"))))
        self.assertEqual(set(second.list_memberships.values_list("list_type", flat=True)), set(TextingListType.values))
        self.assertEqual(AuditEvent.objects.filter(action="account.rep_created").count(), 2)

    def test_nonmanagers_cannot_create_reset_or_assign_memberships(self):
        for actor in [None, self.rep]:
            for operation in [
                lambda: create_rep(actor, "New", "Rep", "new.rep", [TextingListType.GFS]),
                lambda: reset_rep_password(actor, self.rep),
                lambda: update_rep_memberships(actor, self.rep, [TextingListType.GFS]),
            ]:
                with self.assertRaises(PermissionDenied):
                    operation()
        self.manager.is_active = False
        with self.assertRaises(PermissionDenied):
            create_rep(self.manager, "New", "Rep", "new.rep", [TextingListType.GFS])
        self.assertFalse(get_user_model().objects.filter(username="new.rep").exists())

    def test_reset_changes_only_rep_password_and_revokes_prior_session(self):
        rep, original = create_rep(self.manager, "Emilio", "Arguello", "emilio.arguello", [TextingListType.GFS])
        self.client.force_login(rep)
        replacement = reset_rep_password(self.manager, rep)
        rep.refresh_from_db()
        self.assertFalse(rep.check_password(original))
        self.assertTrue(rep.check_password(replacement))
        self.assertFalse(rep.is_staff)
        self.assertEqual(list(rep.list_memberships.values_list("list_type", flat=True)), [TextingListType.GFS])
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)
        event = AuditEvent.objects.get(action="account.rep_password_reset")
        self.assertEqual(event.details, {})
        self.assertNotIn(replacement, json.dumps(event.details))

    def test_manager_accounts_cannot_be_password_or_membership_targets(self):
        with self.assertRaises(PermissionDenied):
            reset_rep_password(self.manager, self.manager)
        with self.assertRaises(PermissionDenied):
            update_rep_memberships(self.manager, self.manager, [TextingListType.GFS])
        self.manager.refresh_from_db()
        self.assertTrue(self.manager.check_password("Keep-Admin-Password-123"))

    def test_membership_updates_are_validated_and_idempotent(self):
        update_rep_memberships(self.manager, self.rep, [TextingListType.GFS])
        update_rep_memberships(self.manager, self.rep, [TextingListType.GFS, TextingListType.GFS])
        self.assertEqual(AuditEvent.objects.filter(action="account.rep_memberships_changed").count(), 1)
        for invalid in [[], ["unknown"], "gfs", None, [[]]]:
            with self.assertRaises(ValidationError):
                update_rep_memberships(self.manager, self.rep, invalid)
        self.assertEqual(list(self.rep.list_memberships.values_list("list_type", flat=True)), [TextingListType.GFS])
        update_rep_memberships(self.manager, self.rep, [TextingListType.RINGCENTRAL])
        self.assertEqual(list(self.rep.list_memberships.values_list("list_type", flat=True)), [TextingListType.RINGCENTRAL])

    def test_creation_rejects_duplicate_and_invalid_identity_without_partial_writes(self):
        for values in [
            ("First", "Last", "EXISTING.REP", [TextingListType.GFS]),
            ("First", "Last", "new.rep", []),
            ("", "Last", "new.rep", [TextingListType.GFS]),
            ("First", "Last", "invalid username", [TextingListType.GFS]),
        ]:
            with self.assertRaises(ValidationError):
                create_rep(self.manager, *values)
        self.assertEqual(get_user_model().objects.count(), 2)
        self.assertFalse(RepListMembership.objects.exists())

    def test_forms_only_accept_identity_and_valid_membership_fields(self):
        form = RepCreationForm({"first_name": "Blake", "last_name": "Fiorito", "username": "blake.fiorito",
                                "list_types": ["gfs"], "is_staff": True, "password1": "Provided-password-123"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(set(form.cleaned_data), {"first_name", "last_name", "username", "list_types"})
        self.assertFalse(RepMembershipForm({"list_types": ["unknown"]}).is_valid())
        update_rep_memberships(self.manager, self.rep, [TextingListType.GFS])
        self.assertEqual(RepMembershipForm(rep=self.rep)["list_types"].value(), [TextingListType.GFS])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ProvisioningTests(TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user("demo.manager", password="Keep-Admin-Password-123", is_staff=True)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "initial-credentials.txt"

    def provision(self, output=None):
        stdout = StringIO()
        call_command("provision_texting_reps", admin=self.manager.username, output=str(output or self.output), stdout=stdout)
        return stdout.getvalue()

    def test_provisions_exact_roster_and_writes_new_credentials_privately(self):
        stdout = self.provision()
        self.assertEqual(get_user_model().objects.filter(is_staff=False).count(), 16)
        self.assertEqual(RepListMembership.objects.filter(list_type=TextingListType.GFS).count(), 12)
        self.assertEqual(RepListMembership.objects.filter(list_type=TextingListType.RINGCENTRAL).count(), 5)
        emilio = get_user_model().objects.get(username="emilio.arguello")
        self.assertEqual(emilio.list_memberships.count(), 2)
        text = self.output.read_text(encoding="utf-8")
        credentials = re.findall(r"Username: ([^\n]+)\nLists: [^\n]+\nPassword: ([^\n]+)", text)
        self.assertEqual(len(credentials), 16)
        self.assertEqual(len({password for _, password in credentials}), 16)
        for username, password in credentials:
            self.assertTrue(get_user_model().objects.get(username=username).check_password(password))
            self.assertNotIn(password, stdout)
            self.assertNotIn(password, json.dumps(list(AuditEvent.objects.values("details"))))
        if os.name != "nt":
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.manager.refresh_from_db()
        self.assertTrue(self.manager.check_password("Keep-Admin-Password-123"))

    def test_repeated_provisioning_preserves_existing_passwords_memberships_and_unrelated_data(self):
        User = get_user_model()
        existing = User.objects.create_user("blake.fiorito", first_name="Blake", last_name="Fiorito",
                                            password="Keep-existing-password-123", is_active=False)
        RepListMembership.objects.create(rep=existing, list_type=TextingListType.RINGCENTRAL)
        unrelated = User.objects.create_user("unrelated.rep", first_name="Unrelated", password="Keep-other-password-123")
        self.provision()
        original_hashes = dict(User.objects.values_list("username", "password"))
        event_count = AuditEvent.objects.count()
        followup = self.output.with_name("second-run.txt")
        stdout = self.provision(followup)
        self.assertIn("Created 0 accounts; preserved 16 existing accounts", stdout)
        self.assertNotIn("Password:", followup.read_text(encoding="utf-8"))
        self.assertEqual(original_hashes, dict(User.objects.values_list("username", "password")))
        self.assertEqual(AuditEvent.objects.count(), event_count)
        existing.refresh_from_db()
        self.assertFalse(existing.is_active)
        self.assertEqual(set(existing.list_memberships.values_list("list_type", flat=True)), set(TextingListType.values))
        self.assertTrue(User.objects.filter(pk=unrelated.pk).exists())
        self.assertNotIn("Username: blake.fiorito", self.output.read_text(encoding="utf-8"))

    def test_mismatched_existing_identity_aborts_without_changes_or_output(self):
        get_user_model().objects.create_user("kip.langat", first_name="Different", last_name="Person")
        with self.assertRaises(CommandError):
            self.provision()
        self.assertEqual(get_user_model().objects.count(), 2)
        self.assertFalse(RepListMembership.objects.exists())
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text("Keep this file", encoding="utf-8")
        with self.assertRaises(CommandError):
            self.provision()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "Keep this file")
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_failed_credentials_write_rolls_back_new_accounts_and_removes_partial_file(self):
        with patch("tracker.management.commands.provision_texting_reps.os.fsync", side_effect=OSError("Disk unavailable")):
            with self.assertRaises(CommandError):
                self.provision()
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertFalse(AuditEvent.objects.exists())
        self.assertFalse(self.output.exists())
