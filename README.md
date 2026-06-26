# messages-contact-creator

Reads a block of messages from an Apple Messages **group chat** and auto-creates
Contacts for senders you don't already have, using the names they typed.

Every iMessage carries the sender's handle (phone/email) **and** the text, so a
message whose text is a name gives us name + contact info in one shot. Classic
use case: "everyone send your name in the group" → import the ones you're missing.

## Requirements
- macOS, Python 3 (stdlib only).
- **Full Disk Access** for your terminal app (to read `~/Library/Messages/chat.db`).
  System Settings → Privacy & Security → Full Disk Access → enable your terminal.
- **Contacts access** for your terminal app (first run will prompt; allow it).

## Setup (once)
```bash
python3 build_name_data.py            # fetch name datasets into data/
```

## Usage
```bash
python3 import_contacts.py            # interactive
python3 import_contacts.py --dry-run  # preview, never writes Contacts
python3 import_contacts.py --show 60  # show last 60 messages when picking a range
python3 import_contacts.py --chat "college"  # filter chat list by name/participant
python3 import_contacts.py --chats 100  # list 100 group chats instead of 25
python3 import_contacts.py --note "Pickup basketball group"  # note on every new contact
python3 import_contacts.py --delay 3  # seconds between creations (default 1.5; 0 disables)
```

> **iCloud sync note:** contacts are created one at a time with a short pause
> (`--delay`, default 1.5s). Creating many contacts in a tight burst can stall
> iCloud's CardDAV sync so they never reach your phone; spacing them out avoids
> that. They're created in whichever account is your Contacts default — make sure
> that's **iCloud** (not "On My Mac" or Google) if you want them on your phone.

Flow:
1. Pick a group chat from the list.
2. See the last N messages, indexed.
3. Enter the start/end index of the block where people sent names.
4. Review the proposed new contacts (name → handle, with confidence).
   - `ok` create selected · `skip N` / `add N` toggle · `edit N New Name`
   - `add-h N First Last` promote an unknown sender that had no name-like message
   - `q` quit without writing
5. Optionally enter a note to attach to every new contact (or `--note "…"`).
6. Confirmed contacts are created in Contacts.app.

## How names are detected
First run `python3 build_name_data.py` once to fetch the name datasets into
`data/` (first + last names; ~20k / ~85k). The detector then, per message:

1. **Cleans** — strips emoji, trailing parentheticals/notes, and filler prefixes,
   so `Alex Kim 😎`, `Jordan Lee (from work, not the other one)`, and `im sam`
   reduce to `Alex Kim`, `Jordan Lee`, and `Sam`.
2. **Rejects sentences** — drops anything too long, slang (`bro`, `ggs`), elongated
   (`Shitttt`), or containing an ordinary word that isn't itself a known name
   (so `can we play` / `Loved an image` are not names).
3. **Recognizes** — by dataset membership (handles lowercase, e.g. `maria lopez`),
   plus a tightly-gated **phonetic** layer (metaphone, via `jellyfish` if present)
   that catches odd spellings not in the dataset (`Jaiden`, `Micheal`, `Kaitlynn`).

Phonetic is gated to Title-Case, non-dictionary tokens — without that gate ~half
of normal chat words collide with a name code. If `data/` is missing it falls
back to structural detection; if `jellyfish` is missing, phonetic recall is off.
Every candidate is still shown for review before anything is created.

## Dedup
Existing contacts are dumped once. Phone numbers are normalized to their last 10
digits and emails are lowercased, so anyone you already have (in any format) is
skipped. Matching is per-handle: if you have someone's email but they text from a
phone, they may still show up — just `skip` them.

## Safety
- `chat.db` is opened **read-only**; the tool never writes to Messages.
- Nothing is written to Contacts until you confirm; `--dry-run` guarantees no writes.
