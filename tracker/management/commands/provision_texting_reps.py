"""Idempotently provision the known texting roster and privately deliver new credentials."""

import os
from pathlib import Path
import unicodedata

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from tracker.accounts import create_rep, update_rep_memberships
from tracker.models import TextingListType
from tracker.services import _phone_transaction, _require_manager


ROSTER = (
    ("Blake", "Fiorito", (TextingListType.GFS,)),
    ("Christian", "Quintana", (TextingListType.GFS,)),
    ("Evan", "Kruer", (TextingListType.GFS,)),
    ("Edward", "Jeudy", (TextingListType.GFS,)),
    ("Jimmy", "Cipriani", (TextingListType.GFS,)),
    ("Emilio", "Arguello", (TextingListType.GFS, TextingListType.RINGCENTRAL)),
    ("John", "Saldarriaga", (TextingListType.GFS,)),
    ("Cristian", "Pina", (TextingListType.GFS,)),
    ("Kevin", "Cohen", (TextingListType.GFS,)),
    ("Richard", "Calderon", (TextingListType.GFS,)),
    ("Alejandro", "Manuel", (TextingListType.GFS,)),
    ("Gabriel", "Sulca", (TextingListType.GFS,)),
    ("Anthony", "Diaz", (TextingListType.RINGCENTRAL,)),
    ("Erik", "Anderson", (TextingListType.RINGCENTRAL,)),
    ("Michael", "Cifuentes", (TextingListType.RINGCENTRAL,)),
    ("Kip", "Langat", (TextingListType.RINGCENTRAL,)),
)


def _identity(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _open_private_credentials(path):
    """Exclusively create a file, private from its first instant on disk."""
    if os.name != "nt":
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    # os.open(mode=0600) does not restrict inherited Windows ACLs. Supply an
    # owner-only, protected DACL at creation instead of correcting it afterward.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.POINTER(SecurityAttributes), wintypes.DWORD,
                       wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    if not convert("D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        handle = create(str(path), 0x40000000, 0, ctypes.byref(attributes), 1, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(handle)
        path.unlink(missing_ok=True)
        raise


class Command(BaseCommand):
    help = "Provision the GFS/Ringcentral roster without resetting existing accounts."

    def add_arguments(self, parser):
        parser.add_argument("--admin", required=True, help="Existing active administrator username.")
        parser.add_argument("--output", required=True, help="Absolute path for a new private credentials text file.")

    def handle(self, *args, **options):
        output = Path(options["output"]).expanduser()
        if not output.is_absolute():
            raise CommandError("--output must be an absolute file path.")
        if output.exists() or output.is_symlink():
            raise CommandError("The credentials output already exists; choose a new file path.")
        if not output.parent.is_dir():
            raise CommandError("The credentials output directory must already exist.")
        created_file = False
        created = []
        existing_count = 0
        try:
            with _phone_transaction():
                User = get_user_model()
                actor = User.objects.select_for_update().filter(username=options["admin"]).first()
                _require_manager(actor)
                # Validate the entire existing roster before creating anything.
                prepared = []
                for first, last, list_types in ROSTER:
                    username = f"{first}.{last}".lower()
                    matches = list(User.objects.select_for_update().filter(username__iexact=username)[:2])
                    if len(matches) > 1:
                        raise CommandError(f"Ambiguous existing username: {username}.")
                    rep = matches[0] if matches else None
                    if rep and (rep.is_staff or rep.is_superuser
                                or _identity(rep.first_name) != _identity(first)
                                or _identity(rep.last_name) != _identity(last)):
                        raise CommandError(f"Existing account identity or role does not match the roster: {username}.")
                    prepared.append((first, last, username, list_types, rep))
                for first, last, username, list_types, rep in prepared:
                    if rep is None:
                        rep, password = create_rep(actor, first, last, username, list_types)
                        created.append((rep, list_types, password))
                    else:
                        previous = set(rep.list_memberships.values_list("list_type", flat=True))
                        update_rep_memberships(actor, rep, previous | set(list_types))
                        existing_count += 1

                descriptor = _open_private_credentials(output)
                created_file = True
                labels = dict(TextingListType.choices)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write("PhoneTracker — initial representative credentials\n")
                    handle.write("Private: share each account only with its assigned representative.\n")
                    handle.write("Existing account passwords were not changed.\n\n")
                    for rep, list_types, password in created:
                        handle.write(f"Name: {rep.get_full_name()}\nUsername: {rep.username}\n")
                        handle.write("Lists: " + ", ".join(labels[value] for value in list_types) + "\n")
                        handle.write(f"Password: {password}\n\n")
                    if not created:
                        handle.write("No new accounts were created. There are no new passwords to share.\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except Exception as error:
            if created_file:
                output.unlink(missing_ok=True)
            if isinstance(error, CommandError):
                raise
            if isinstance(error, (PermissionDenied, ValidationError)):
                raise CommandError(" ".join(getattr(error, "messages", [str(error)]))) from error
            if isinstance(error, OSError):
                raise CommandError("Could not create the private credentials output; no accounts were provisioned.") from error
            raise
        self.stdout.write(self.style.SUCCESS(
            f"Created {len(created)} accounts; preserved {existing_count} existing accounts. Credentials: {output}"
        ))
