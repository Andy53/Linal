"""Read the phone's address book (evolution-data-server, shared with GNOME
Contacts, Calls and Chatty) without extra dependencies.

EDS keeps each address book as SQLite with one vCard per row. We open it
read-only and parse just FN and TEL, so the GUI can offer phone contacts in
the new-chat picker. Pure parsing helpers are unit-tested.
"""
from __future__ import annotations
import glob
import os
import re
import sqlite3
from dataclasses import dataclass
from .registration import normalize_number

EDS_DIR = os.path.expanduser("~/.local/share/evolution/addressbook")


@dataclass
class PhoneContact:
    name: str
    numbers: list[str]      # E.164


def _unfold(vcard: str) -> list[str]:
    # vCard line folding: a line starting with space/tab continues the previous one
    lines: list[str] = []
    for raw in vcard.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").replace("\\\\", "\\")


def parse_vcard(vcard: str) -> PhoneContact | None:
    name = ""
    numbers: list[str] = []
    for line in _unfold(vcard):
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        prop = head.split(";", 1)[0].upper()
        if prop == "FN":
            name = _unescape(value).strip()
        elif prop == "TEL":
            digits = re.sub(r"[^\d+]", "", value)
            if len(digits.lstrip("+")) >= 7:
                n = normalize_number(digits)
                if n not in numbers:
                    numbers.append(n)
    if not numbers:
        return None
    return PhoneContact(name=name or numbers[0], numbers=numbers)


def load_contacts(base: str = EDS_DIR) -> list[PhoneContact]:
    """All contacts with at least one phone number, sorted by name. Returns []
    if there is no address book or it can't be read."""
    out: list[PhoneContact] = []
    for db in glob.glob(os.path.join(base, "*", "contacts.db")):
        if os.sep + "trash" + os.sep in db:
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            rows = con.execute("SELECT vcard FROM folder_id").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for (vc,) in rows:
            c = parse_vcard(vc or "")
            if c:
                out.append(c)
    out.sort(key=lambda c: c.name.lower())
    return out
