# CURRENT STATE — messages-contact-creator

Updated: 2026-06-24

## Where we are right now
Working CLI (`import_contacts.py`, Python stdlib + optional `jellyfish`). Reads a
chosen Messages group chat, interactively pick a block of messages, recognizes
the name each unknown sender typed (robust to emoji/notes/odd spellings), dedups
against existing Contacts **by handle**, and creates the missing contacts after
human review. Four real-use bugs fixed (contacts-read crash, only-4-messages,
duplicate-of-existing, weak name recognition).

## What's in place
- ✅ Read chat.db read-only (mode=ro, WAL-aware); list group chats; fetch messages.
- ✅ Decode `attributedBody` typedstream so modern messages (text NULL) show.
- ✅ Name recognition: clean (strip emoji/notes/filler) → reject sentences/slang →
  dataset match (first ~20k / last ~85k, lowercase-tolerant) → gated metaphone
  phonetic for odd spellings. `build_name_data.py` fetches the datasets.
- ✅ Dedup by normalized handle; contacts read WAL-aware with copy fallback (~423).
- ✅ Contacts created via osascript (one at a time, gated by review).
- ✅ Interactive review (ok/skip/add/edit/add-h/q), `--dry-run`, returns clean names.

## What's NOT in place
- ❌ No name-based dedup — by design (different number ⇒ new contact).
- ❌ One name per message assumed; multi-name messages ("Alex Jordan Sam") not
  special-cased (per user, an edge that doesn't matter).
- ❌ Lone lowercase names not in the dataset (e.g. an uncommon first name alone)
  can be missed; capitalized or full-name forms are caught. Promote via `add-h`.
- ❌ No "merge new handle into existing contact" (only creates new people).

## Recent decisions (sticky)
- 2026-06-24: dedup by handle ONLY; AddressBook opened WAL-aware (never immutable).
- 2026-06-24: name detection assumes the parsed block is almost all name messages
  → recall-first; phonetic gated to Title-Case non-dictionary tokens to avoid a
  ~52% false-positive rate on ordinary chat words.
- 2026-06-24: name datasets bundled in data/ via build_name_data.py; jellyfish
  optional (phonetic disabled gracefully if absent).

## Companion docs
project_log.md (chronological), README.md (usage).
