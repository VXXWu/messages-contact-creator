# Project Log — messages-contact-importer

## 2026-06-24 — Initial build
- Goal: from an Apple Messages group chat, read a block of messages where people
  sent their names and auto-create Contacts for handles not already saved.
- Verified feasibility on this machine: chat.db readable (Full Disk Access already
  granted, 149k messages), `sqlite3`/`python3`/`osascript` present, Contacts read
  via osascript works (414 existing handles).
- Key data insight: each message row has both sender handle (phone/email) and text
  → name + contact info paired. Confirmed a real example in the data
  (`+17083089099` sent "Aydin Alsan").
- Design decisions (from user):
  - Form: Python CLI, stdlib only.
  - Block selection: interactive pick (list recent messages w/ indices, choose
    start/end range).
  - Name detection: heuristic (≤3 letter-led words, no digits/URLs/punct, Title
    Case + dictionary demotion), automated, with human review at the end.
- Implemented `import_contacts.py`:
  - chat.db opened read-only (uri mode=ro).
  - Group chats listed by `style=43`, ordered by last activity.
  - Heuristic `name_score()`, handle dedup via `normalize_handle()`
    (phone→last 10 digits, email→lowercased).
  - Contacts read/create via `osascript` (argv-passed values, no string injection).
  - Interactive review loop (ok/skip/add/edit/add-h/q) + `--dry-run`.
- Tested: name heuristic on positive/negative cases, handle normalization across
  phone formats, DB reads, and read-only Contacts dump. All pass. Live contact
  creation NOT exercised (gated behind user confirmation; avoided modifying the
  real address book).

## Open ideas / not done
- Optional tiny local LLM classifier instead of heuristic (user mentioned).
- Cross-handle dedup (match a phone sender against an email-only contact).
- Merge into existing contact instead of creating, when name matches.

## 2026-06-24 — Fix: contacts read crashed the machine
- Symptom: running the tool hung/crashed the Mac at the "read contacts" stage.
- Root cause: contacts were read by scripting the Contacts app via osascript
  (`repeat with p in people` + nested loops). This launches the app and is
  slow/memory-heavy; it beachballed the system despite working in isolated tests.
- Fix: replaced the osascript read with direct read-only SQLite queries of the
  AddressBook DBs (`~/Library/Application Support/AddressBook/AddressBook-v22.abcddb`
  plus `Sources/*/AddressBook-v22.abcddb`). Opens with `mode=ro&immutable=1`
  (no locks). Reads 412 handles instantly, no app launch.
- osascript is now used ONLY for contact *creation* (one person at a time, gated
  by review) — a lightweight, supported write path.

## 2026-06-24 — Fix: only ~4 messages shown (attributedBody)
- Symptom: message list always showed a handful of messages, even with --show 60.
- Root cause: recent macOS stores text in the `attributedBody` blob, leaving
  message.text NULL. In the busiest chat only 8/21646 rows had non-null text, so
  the `text IS NOT NULL` filter discarded ~all messages.
- Fix: added `decode_attributed_body()` (typedstream: NSString marker → '+' type
  code → 1/2/4-byte length prefix → UTF-8). `fetch_messages` now decodes the blob
  when text is empty, strips ￼ attachment placeholders, skips empties, and
  over-fetches (cap = max(limit*6, 240)) so --show N yields N real messages.
- Verified: --dry-run --show 60 now lists 60 decoded messages (was 8).

## 2026-06-24 — Fix: duplicate created for a contact you already had (stale WAL read)
- Symptom: tool created "Jake lee" (+13174296536) even though contact "Jake Lee"
  with that exact number already existed.
- Diagnosis: handle dedup is correct; the contacts READ was stale. AddressBook
  DBs were opened with `mode=ro&immutable=1`. `immutable=1` makes SQLite ignore
  the `-wal` file, so recently-saved/edited contacts (still in the WAL, here
  3.1MB + 424KB of pending changes) were invisible → treated as new → duplicated.
  Proven: immutable read = 360 phone rows vs WAL-aware read = 359.
- Fix: `_open_ro()` now opens `mode=ro` (WAL-aware), with a copy-based fallback
  (copy db+wal+shm to a scratch dir, read the copy) for setups where a read-only
  WAL open fails; returns (con, cleanup). chat.db was already mode=ro (fine).
- Also removed the name-dedup helpers added earlier: per user, same name +
  different number SHOULD create a new contact; dedup must be by handle only.
- Verified: WAL-aware read sees 423 handles (was 412 stale); Jake now in existing
  and excluded from would-create over the whole pic chat.

## 2026-06-24 — Smarter name recognition (datasets + gated phonetic)
- Goal: recognize names robustly despite noise ("Tristan Zhu 😎", "Jayden Chen
  (from Robinson…)", "im jacob") and odd spellings (Jaiden/Aydin).
- Added build_name_data.py: fetches first names (~20k) + surnames (~85k) from
  smashew/NameDatabases into data/{first_names,last_names}.txt (normalized).
- New name_score pipeline:
  1. _name_tokens(): strip filler prefixes ("im","this is"), cut at emoji/parens/
     notes, take leading name tokens.
  2. Hard rejects: >3 tokens, elongated slang (3+ repeated char), STOPWORDS
     (chat/tapback/slang), or any token that is a common English word (incl.
     simple inflections via is_wordish) and NOT a known name → kills sentence
     fragments like "can we play".
  3. Scoring: dataset membership (primary, handles lowercase "tony gao"); gated
     metaphone phonetic (jellyfish, optional) for odd spellings — gated to
     Title-Case non-dictionary tokens because ungated it false-fires on ~52% of
     chat words; recall-first lone-proper-noun acceptance.
  Returns (is_name, conf, clean_name); main() now stores the cleaned name.
- Findings that drove the design: dataset misses variants (jaiden/aydin/nevaeh)
  but metaphone recovers all of them; ungated phonetic FP rate 52% on chat words,
  ~0 after the Title-Case+not-dictionary gate. Real-data flag rate 17%→8.2%
  (remaining flags are general-conversation msgs outside the target block).
- Per user: assume the parsed block is almost entirely name messages; multi-name
  msgs ("Zarni Vince Linden") are edge and not specially handled.
- Verified end-to-end on the pic chat: "Jayden Chen (from…)"→Jayden Chen,
  "Tristan Zhu 😎"→Tristan Zhu, "im jacob"→Jacob.

## 2026-06-24 — Feature: shared note on all new contacts
- After review (before creating), prompt for an optional note applied to every new
  contact; or pass --note "..." to skip the prompt (--note "" forces none).
- Verified AppleScript can set the Contacts Notes field on this machine (Apple has
  restricted note access on some setups; tested create+readback+delete = works).
- CREATE_SCRIPT now takes a 5th argv (note) and sets `note of p` when non-empty;
  create_contact(name, handle, note=""). Dry-run prints the note too.
- Verified end-to-end: created a contact via create_contact() with a note, read it
  back, deleted it.
