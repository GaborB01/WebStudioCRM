"""Deciding which client and project an email belongs to.

The input is a MatchInput - plain data, the same whichever way the mail
came in (IMAP, an inbound webhook, a folder of .eml files in development).
The output is a MatchResult; nothing is written until apply_match().

Rules run strongest first and the first with an answer wins:

1. REFERENCE  a project, quote or document number in the subject - or,
              if the subject has none, in the body outside quoted lines
2. THREAD     a reply to a message that is already matched
3. CONTACT    an external party is a known contact address
4. DOMAIN     an external party writes from a client's registered domain

Later rules still run as a cross-check: a reference to one client's
project in mail from another client's contact is flagged for review
rather than silently trusted. DOMAIN matches are always flagged, since
they are inferences about people the CRM has never seen.

"External parties" are the addresses that are not ignored (see
core.models.EmailIgnoreRule): the sender of received mail, then the
other recipients; the recipients of sent mail. The studio's own
addresses are never a client.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from django.db.models import Q

from clients.models import Client, Contact
from core.emails import normalize_email
from core.models import EmailIgnoreRule

from ..choices import CONFIDENT_METHODS, Direction, MatchMethod

#: Project statuses after which new mail is unlikely to be about the project
CLOSED_PROJECT_STATUSES = ("completed", "cancelled")


@dataclass(frozen=True)
class MatchInput:
    direction: str
    from_address: str
    to: tuple = ()
    cc: tuple = ()
    bcc: tuple = ()
    subject: str = ""
    body_text: str = ""
    in_reply_to: str = ""
    references: tuple = ()
    #: The message being matched, if stored already: never its own ancestor
    message_id: str = ""

    @classmethod
    def from_message(cls, message):
        def emails(entries):
            return tuple(e.get("email", "") for e in entries)

        return cls(
            direction=message.direction,
            from_address=message.from_address,
            to=emails(message.to_addresses),
            cc=emails(message.cc_addresses),
            bcc=emails(message.bcc_addresses),
            subject=message.subject,
            body_text=message.body_text,
            in_reply_to=message.in_reply_to,
            references=tuple(message.references),
            message_id=message.message_id,
        )

    @property
    def external_parties(self):
        """Addresses that could be the client side, most telling first."""
        if self.direction == Direction.INBOUND:
            ordered = [self.from_address, *self.cc, *self.to]
        else:
            ordered = [*self.to, *self.cc, *self.bcc]
        seen = []
        for address in map(normalize_email, ordered):
            if address and address not in seen and not EmailIgnoreRule.ignores(address):
                seen.append(address)
        return seen


@dataclass
class MatchResult:
    method: str = MatchMethod.UNMATCHED
    client: Client = None
    project: object = None
    contacts: list = field(default_factory=list)
    needs_review: bool = False
    notes: list = field(default_factory=list)

    @property
    def is_matched(self):
        return self.client is not None

    @property
    def note(self):
        return "; ".join(self.notes)[:500]

    def flag(self, reason):
        self.needs_review = True
        self.notes.append(reason)


# ---- rule 1: reference numbers ----------------------------------------------


def _reference_pattern():
    """PRJ-2026-0001, Q-2026-0003-v2, NDA-2026-0001, DEV-AGR-2026-0002...

    Built from the prefixes in use, longest first so DEV-AGR wins over DEV.
    Case-insensitive: people retype numbers by hand.
    """
    from documents.models import DocumentType
    from projects.models import PROJECT_PREFIX
    from quotes.services.workflow import QUOTE_PREFIX

    prefixes = {PROJECT_PREFIX, QUOTE_PREFIX}
    prefixes.update(p.upper() for p in DocumentType.objects.values_list("prefix", flat=True) if p)
    alternatives = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    return re.compile(
        rf"(?<![A-Za-z0-9-])(?:{alternatives})-\d{{4}}-\d{{4,}}(?:-v\d+)?(?![A-Za-z0-9-])",
        re.IGNORECASE,
    )


def _unquoted(body):
    """The body without quoted reply lines, which repeat older references."""
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))


def _normalize_token(token):
    """The form numbers are stored in: upper case, revision as "-v2"."""
    return re.sub(r"-V(\d+)$", r"-v\1", token.upper())


def find_references(subject, body_text):
    """Reference tokens in the subject; the body is only read if there are none."""
    pattern = _reference_pattern()
    tokens = pattern.findall(subject or "") or pattern.findall(_unquoted(body_text or ""))
    return list(dict.fromkeys(map(_normalize_token, tokens)))  # unique, in order


def _resolve_reference(token):
    """(client, project) for a reference token, or None if it names nothing."""
    from documents.models import Document
    from projects.models import PROJECT_PREFIX, Project
    from quotes.models import Quote

    base = re.sub(r"-v\d+$", "", token)
    if token.startswith(f"{PROJECT_PREFIX}-"):
        project = Project.objects.select_related("client").filter(number=base).first()
        return (project.client, project) if project else None

    for model in (Quote, Document):
        record = (
            model.objects.filter(number__in=[token, base])
            .order_by("-number")  # the exact revision before the base number
            .first()
        )
        if record is None:
            continue
        if model is Quote:
            return record.project.client, record.project
        return record.client, record.project
    return None


def _match_reference(data, result):
    targets = []
    for token in find_references(data.subject, data.body_text):
        resolved = _resolve_reference(token)
        if resolved:
            targets.append((token, *resolved))
    if not targets:
        return False

    token, client, project = targets[0]
    result.method, result.client, result.project = MatchMethod.REFERENCE, client, project
    result.notes.append(f"Reference {token}")

    others = {(c.pk, p.pk if p else None) for _, c, p in targets[1:]} - {
        (client.pk, project.pk if project else None)
    }
    if others:
        result.flag(f"Several references: {', '.join(t for t, _, _ in targets)}")
    return True


# ---- rule 2: thread ---------------------------------------------------------


def _match_thread(data, result):
    from ..models import EmailMessage

    # Nearest ancestor first: In-Reply-To, then References from the end
    ancestors = [data.in_reply_to, *reversed(data.references)]
    ancestors = [a for a in dict.fromkeys(ancestors) if a and a != data.message_id]
    if not ancestors:
        return False

    matched = {
        m.message_id: m
        for m in EmailMessage.objects.filter(message_id__in=ancestors, client__isnull=False)
        .select_related("client", "project")
    }
    for message_id in ancestors:
        parent = matched.get(message_id)
        if parent is not None:
            result.method, result.client, result.project = (
                MatchMethod.THREAD, parent.client, parent.project,
            )
            result.notes.append(f"Reply to {message_id}")
            if parent.needs_review:
                result.flag("The email it replies to is still awaiting review")
            return True
    return False


# ---- rules 3 and 4: who is on the message -----------------------------------


def _contacts_by_party(parties):
    """{address: [contacts]} for every party that is a known contact."""
    found = {}
    contacts = (
        Contact.objects.filter(
            Q(email__in=parties) | Q(email_addresses__email__in=parties)
        )
        .select_related("client")
        .prefetch_related("email_addresses")
        .distinct()
    )
    for contact in contacts:
        for address in contact.all_emails:
            if address in parties:
                found.setdefault(address, []).append(contact)
    return found


def _pick_client(parties, clients_by_party):
    """The client most parties point at; ties go to the most telling party.

    Returns (client, ambiguous).
    """
    votes = Counter()
    first_seen = {}
    for position, address in enumerate(parties):
        for client in {c.pk: c for c in clients_by_party.get(address, [])}.values():
            votes[client.pk] += 1
            first_seen.setdefault(client.pk, (position, client))
    if not votes:
        return None, False
    best = max(votes, key=lambda pk: (votes[pk], -first_seen[pk][0]))
    return first_seen[best][1], len(votes) > 1


def _open_project_for(client, contacts):
    """The project a client-only match belongs to, when that is obvious.

    The client's only open project; failing that, the one open project a
    contact on the message is the primary contact of.
    """
    open_projects = list(client.projects.exclude(status__in=CLOSED_PROJECT_STATUSES))
    if len(open_projects) == 1:
        return open_projects[0]
    contact_ids = {c.pk for c in contacts}
    led = [p for p in open_projects if p.primary_contact_id in contact_ids]
    return led[0] if len(led) == 1 else None


def _match_people(data, result, contacts_by_party):
    parties = data.external_parties
    clients_by_party = {a: [c.client for c in cs] for a, cs in contacts_by_party.items()}

    client, ambiguous = _pick_client(parties, clients_by_party)
    if client is not None:
        result.method, result.client = MatchMethod.CONTACT, client
        named = [a for a in parties if a in contacts_by_party]
        result.notes.append(f"Contact {named[0]}")
        if ambiguous:
            names = sorted({c.name for cs in clients_by_party.values() for c in cs})
            result.flag(f"Contacts of several clients: {', '.join(names)}")
        return True

    domain_clients = {a: list(Client.objects.with_email_domain(a)) for a in parties}
    client, ambiguous = _pick_client(parties, domain_clients)
    if client is None:
        return False
    result.method, result.client = MatchMethod.DOMAIN, client
    address = next(a for a in parties if domain_clients[a])
    result.flag(f"Unknown sender {address} at {client.name}'s domain")
    if ambiguous:
        result.flag("Domains of several clients")
    return True


# ---- putting it together ----------------------------------------------------


def match(data):
    """Run the rules on a MatchInput and return the MatchResult."""
    result = MatchResult()
    contacts_by_party = _contacts_by_party(data.external_parties)

    if _match_reference(data, result) or _match_thread(data, result):
        # Cross-check against the people on the message
        people_clients = {c.client_id for cs in contacts_by_party.values() for c in cs}
        if people_clients and result.client.pk not in people_clients:
            result.flag(
                f"{result.method.label} points at {result.client.name}, "
                "but no contact on the email belongs to it"
            )
    elif not _match_people(data, result, contacts_by_party):
        result.notes.append("No reference, matched thread, contact or client domain")
        return result

    result.contacts = sorted(
        {c.pk: c for cs in contacts_by_party.values() for c in cs
         if c.client_id == result.client.pk}.values(),
        key=lambda c: c.pk,
    )
    if result.project is None:
        result.project = _open_project_for(result.client, result.contacts)
        if result.project is not None:
            result.notes.append(f"Only open project {result.project.number}")
    return result


def apply_match(message, result, reason=""):
    """Store a MatchResult on a message. Manual assignments are left alone.

    History is never thrown away: a message that was matched before and
    matches nothing now keeps its client, becomes STALE and is flagged, so
    a person decides whether it stays, moves or goes. `reason` says why
    the match was re-run ("Contact Anna K was deleted") and is kept in the
    note, on the message and on its timeline entry.

    Returns True when the message was changed.
    """
    if message.match_method == MatchMethod.MANUAL:
        return False

    notes = [reason] if reason else []
    if result.is_matched:
        if message.client_id and message.client_id != result.client.pk:
            notes.append(f"Moved here from {message.client.name}")
        notes.extend(result.notes)
        message.client = result.client
        message.project = result.project
        message.match_method = result.method
        message.needs_review = result.needs_review or result.method not in CONFIDENT_METHODS
        contacts = result.contacts
    elif message.client_id:
        notes.append(
            f"Nothing matches any more; left with {message.client.name}. "
            "Confirm it, move it, or delete it."
        )
        message.match_method = MatchMethod.STALE
        message.needs_review = True
        contacts = []
    else:
        notes.extend(result.notes)
        message.match_method = MatchMethod.UNMATCHED
        message.needs_review = False
        contacts = []

    message.match_note = "; ".join(notes)[:500]
    message.save()
    message.contacts.set(contacts)
    return True


def match_message(message, reason=""):
    """Match a stored message and save the outcome; returns the MatchResult."""
    result = match(MatchInput.from_message(message))
    apply_match(message, result, reason=reason)
    return result
