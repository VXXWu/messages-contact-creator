#!/usr/bin/env python3
"""
Read a block of messages from an Apple Messages group chat and auto-create
Contacts for senders you don't already have, using the names they sent.

Each message carries both the sender's handle (phone/email) and the text, so a
message whose text is a name gives us name + contact info in one shot.

Pure stdlib: sqlite3 reads chat.db AND the AddressBook DBs (fast, read-only);
osascript is used ONLY to create new contacts (one at a time, after you confirm).
Requires: Full Disk Access (read both DBs) and Contacts access (create prompts).
"""
import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DB_DEFAULT = Path.home() / "Library/Messages/chat.db"
APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds

ADDRESSBOOK_DIR = Path.home() / "Library/Application Support/AddressBook"

# Lowercase tokens that look name-shaped but almost never are names. Includes the
# fixed set of iMessage tapback verbs (Loved/Liked/.../Reacted) so "Loved an image"
# is not mistaken for a name.
STOPWORDS = {
    "ok", "okay", "yes", "no", "yeah", "yep", "nope", "nah", "lol", "lmao",
    "haha", "hahaha", "hi", "hey", "hello", "yo", "sup", "thanks", "thx", "ty",
    "sure", "cool", "nice", "wow", "omg", "wtf", "idk", "imo", "true", "same",
    "done", "here", "ready", "good", "morning", "night", "bye", "later", "what",
    "why", "how", "when", "who", "where", "please", "sorry", "wait", "stop",
    "going", "coming", "maybe", "today", "tomorrow", "tonight", "now",
    "loved", "liked", "disliked", "laughed", "emphasized", "questioned", "reacted",
    # chat slang that can look name-shaped
    "bro", "bruh", "man", "ya", "yea", "ye", "alr", "ight", "aight", "ggs", "gg",
    "yk", "fr", "ong", "smh", "idc", "nvm", "tbh", "rn", "yall", "ur", "u", "lemme",
    "gonna", "wanna", "dunno", "naw", "mmm", "huh", "oop", "oof", "ngl", "deadass",
}

ELONGATED = re.compile(r"(.)\1\1")  # 3+ repeated chars: "Shitttt", "Yesss"

# Words that end a name and start a note/aside, e.g. "Jordan Lee from work".
CONNECTOR_STOPS = {
    "from", "the", "at", "in", "of", "on", "last", "year", "not", "aka", "and",
    "a", "an", "my", "is", "im", "with", "for", "to", "this", "that", "but",
}

# Filler prefixes people prepend to a name, e.g. "im jacob", "this is Maria".
FILLER_PREFIX = re.compile(
    r"^(?:i['’]?m|im|i\s+am|my\s+name\s+is|name['’]?s|name\s+is|this\s+is|"
    r"it['’]?s|its|hey|hi|hello|yo|call\s+me)\b[\s,:.\-]*",
    re.IGNORECASE,
)
TRUNCATE_AT = "([{|/<>\n\r\t"  # everything after one of these is a note, not the name

DATA_DIR = Path(__file__).resolve().parent / "data"


# ----------------------------- name datasets / phonetics -----------------------------
def _load_set(path):
    p = Path(path)
    if not p.exists():
        return set()
    try:
        return {w.strip().lower() for w in p.read_text(errors="ignore").splitlines() if w.strip()}
    except OSError:
        return set()


def load_dict_words():
    return _load_set("/usr/share/dict/words")


DICT = load_dict_words()
FIRST_NAMES = _load_set(DATA_DIR / "first_names.txt")
LAST_NAMES = _load_set(DATA_DIR / "last_names.txt")

try:
    import jellyfish as _jf

    def _phonetic(s):
        return _jf.metaphone(s)

    HAVE_PHONETIC = True
except Exception:  # jellyfish not installed -> phonetic recall disabled
    HAVE_PHONETIC = False

    def _phonetic(s):
        return ""


# Phonetic codes of every known first name; lets us recognize odd spellings
# (Jaiden/Jaeden/Micheal) that aren't literally in the dataset. Built once.
FIRST_NAME_CODES = {_phonetic(n) for n in FIRST_NAMES} if HAVE_PHONETIC else set()

_DEINFLECT = [("s", ""), ("es", ""), ("ed", ""), ("d", ""), ("ing", ""),
              ("ing", "e"), ("ed", "e"), ("ies", "y"), ("er", ""), ("ly", "")]


def is_wordish(w):
    """True if w (lowercased) is a common English word or a simple inflection of one.
    Used to keep the phonetic path from accepting words like 'loved'/'sounds'."""
    if w in DICT:
        return True
    for suf, repl in _DEINFLECT:
        if w.endswith(suf):
            base = w[: -len(suf)] + repl
            if len(base) >= 2 and base in DICT:
                return True
    return False


def smart_title(name):
    """Title-case a typed name but preserve internal caps (McDonald, O'Brien)."""
    out = []
    for tok in name.split():
        out.append(tok if any(c.isupper() for c in tok[1:]) else tok[:1].upper() + tok[1:])
    return " ".join(out)


def _name_tokens(text):
    """Strip filler/emoji/notes and return the leading run of name tokens (<=3)."""
    t = (text or "").strip()
    if not t:
        return []
    for _ in range(2):  # peel up to two stacked fillers ("hey im jacob")
        m = FILLER_PREFIX.match(t)
        if not m:
            break
        t = t[m.end():]
    cut = len(t)
    for ch in TRUNCATE_AT:
        i = t.find(ch)
        if i != -1:
            cut = min(cut, i)
    i = t.find(" - ")
    if i != -1:
        cut = min(cut, i)
    t = t[:cut]
    # keep letters/space/hyphen/apostrophe/period; turn everything else (emoji,
    # punctuation, digits) into spaces so it acts as a token boundary.
    t = "".join(ch if (ch.isalpha() or ch in " -'’.") else " " for ch in t)

    toks = []
    for tok in t.split():
        low = tok.lower().strip(".'’-")
        if not low or low in CONNECTOR_STOPS or not tok[0].isalpha():
            break
        toks.append(tok.strip(".'’"))
        if len(toks) >= 4:  # collect one past the limit so we can detect "too long"
            break
    return toks


def _known_name(x):
    return x in FIRST_NAMES or x in LAST_NAMES


def name_score(text):
    """Return (is_name, confidence 0..1, clean_name).

    Strategy: extract the leading name tokens (after stripping emoji/notes/filler),
    then HARD-REJECT anything that looks like a sentence — too long, slang, or
    containing an ordinary English word that isn't itself a known name. What
    survives is scored by dataset membership, with a tightly-gated phonetic layer
    for odd spellings (Jaiden/Micheal) and recall-first acceptance of a lone
    capitalized proper noun.
    """
    toks = _name_tokens(text)
    clean = smart_title(" ".join(toks)) if toks else ""
    if not toks or len(toks) > 3:
        return False, 0.0, clean
    low = [t.lower() for t in toks]
    titles = [t[:1].isupper() for t in toks]

    # hard rejects -> it's a phrase/sentence, not a name
    if any(ELONGATED.search(t) for t in low):
        return False, 0.0, clean
    if any(x in STOPWORDS for x in low):
        return False, 0.0, clean
    if any(is_wordish(x) and not _known_name(x) for x in low):
        return False, 0.0, clean

    first = low[0]
    conf = 0.0
    if first in FIRST_NAMES:
        conf += 0.6
    elif titles[0] and not is_wordish(first) and HAVE_PHONETIC and _phonetic(first) in FIRST_NAME_CODES:
        conf += 0.45  # odd spelling that sounds like a known first name
    if all(_known_name(x) for x in low):
        conf += 0.3  # every token is a real name (covers lowercase "maria lopez")
    if len(toks) >= 2 and all(titles):
        conf += 0.3  # proper "First Last" capitalization
    if len(toks) >= 2 and any(_known_name(x) for x in low[1:]):
        conf += 0.15
    if len(toks) == 1 and titles[0] and not is_wordish(first) and first not in FIRST_NAMES and len(first) >= 2:
        conf += 0.5  # lone capitalized proper noun; recall-first (people send first names)

    conf = max(0.0, min(1.0, conf))
    return conf >= 0.5, conf, clean


# ----------------------------- handle normalize -----------------------------
def normalize_handle(h):
    h = (h or "").strip()
    if not h:
        return None
    if "@" in h:
        return "e:" + h.lower()
    digits = re.sub(r"\D", "", h)
    if not digits:
        return None
    if len(digits) > 10:
        digits = digits[-10:]
    return "p:" + digits


# ----------------------------- Contacts (osascript) -----------------------------
def run_osascript(script, args=None):
    cmd = ["osascript", "-", *(args or [])]
    proc = subprocess.run(cmd, input=script, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "osascript failed")
    return proc.stdout


CREATE_SCRIPT = '''
on run argv
    set fn to item 1 of argv
    set ln to item 2 of argv
    set h to item 3 of argv
    set htype to item 4 of argv
    set noteText to item 5 of argv
    tell application "Contacts"
        set p to make new person with properties {first name:fn, last name:ln}
        if noteText is not "" then set note of p to noteText
        if htype is "email" then
            make new email at end of emails of p with properties {label:"home", value:h}
        else
            make new phone at end of phones of p with properties {label:"mobile", value:h}
        end if
        save
    end tell
end run
'''


def _addressbook_dbs():
    """All AddressBook SQLite files: the top-level one plus every account source."""
    dbs = []
    main = ADDRESSBOOK_DIR / "AddressBook-v22.abcddb"
    if main.exists():
        dbs.append(main)
    dbs.extend(sorted((ADDRESSBOOK_DIR / "Sources").glob("*/AddressBook-v22.abcddb")))
    return dbs


def _open_ro(path):
    """Open a SQLite DB read-only, honoring the WAL, returning (con, cleanup).

    IMPORTANT: do NOT use immutable=1 here. immutable tells SQLite the file never
    changes, so it ignores the -wal file and reads a stale snapshot — which caused
    recently-saved contacts to be invisible and get duplicated. mode=ro reads the
    latest committed state including the WAL. If a direct read-only open fails
    (some setups can't open a read-only WAL db in place), fall back to copying
    db+wal+shm to a scratch dir and reading the copy.
    """
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("SELECT 1")  # force open; raises here if unusable
        return con, con.close
    except sqlite3.OperationalError:
        tmp = tempfile.mkdtemp(prefix="ab_")
        for ext in ("", "-wal", "-shm"):
            src = Path(str(path) + ext)
            if src.exists():
                shutil.copy2(src, Path(tmp) / src.name)
        con = sqlite3.connect(str(Path(tmp) / Path(path).name))

        def cleanup():
            con.close()
            shutil.rmtree(tmp, ignore_errors=True)

        return con, cleanup


def load_existing_contacts():
    """Return (normalized_handle_set, {normalized_handle: full_name}).

    Reads the AddressBook SQLite DBs directly (read-only). This deliberately does
    NOT script the Contacts app, which is slow and memory-heavy on large books.
    """
    handles, names = set(), {}

    def record_name(first, last, org):
        nm = " ".join(p for p in (first, last) if p) or (org or "")
        return nm.strip()

    for db in _addressbook_dbs():
        try:
            con, cleanup = _open_ro(db)
        except sqlite3.OperationalError:
            continue
        try:
            queries = [
                ("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                ("ZABCDEMAILADDRESS", "ZADDRESS"),
            ]
            for table, col in queries:
                try:
                    rows = con.execute(
                        f"""
                        SELECT t.{col}, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION
                        FROM {table} t JOIN ZABCDRECORD r ON r.Z_PK = t.ZOWNER
                        WHERE t.{col} IS NOT NULL
                        """
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue
                for value, first, last, org in rows:
                    key = normalize_handle(value)
                    if not key:
                        continue
                    handles.add(key)
                    nm = record_name(first, last, org)
                    if nm:
                        names.setdefault(key, nm)
        finally:
            cleanup()

    return handles, names


def create_contact(name, handle, note=""):
    parts = name.split()
    first = parts[0]
    last = " ".join(parts[1:])
    htype = "email" if "@" in handle else "phone"
    run_osascript(CREATE_SCRIPT, [first, last, handle, htype, note])


# ----------------------------- chat.db reads -----------------------------
def connect(db_path):
    if not Path(db_path).exists():
        sys.exit(f"chat.db not found at {db_path}")
    try:
        # read-only so we never touch the live DB
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        sys.exit(f"Could not open chat.db ({e}). Grant Terminal Full Disk Access.")


def list_group_chats(con, limit=25, query=None):
    """Most-recent group chats. If query is given, filter to chats whose display
    name OR a participant handle contains it (so you can reach chats far down the
    recency list without scrolling past the limit)."""
    q = f"%{query}%" if query else None
    rows = con.execute(
        """
        SELECT c.ROWID, c.display_name, group_concat(h.id, ', ')
        FROM chat c
        JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        JOIN handle h ON h.ROWID = chj.handle_id
        WHERE c.style = 43
          AND (?2 IS NULL
               OR c.display_name LIKE ?2
               OR EXISTS (SELECT 1 FROM chat_handle_join j JOIN handle hh ON hh.ROWID = j.handle_id
                          WHERE j.chat_id = c.ROWID AND hh.id LIKE ?2))
        GROUP BY c.ROWID
        ORDER BY (
            SELECT max(m.date) FROM chat_message_join cmj
            JOIN message m ON m.ROWID = cmj.message_id
            WHERE cmj.chat_id = c.ROWID
        ) DESC
        LIMIT ?1
        """,
        (limit, q),
    ).fetchall()
    return rows


def decode_attributed_body(data):
    """Extract message text from the typedstream `attributedBody` blob.

    Recent macOS leaves message.text NULL and stores the text in attributedBody
    (an NSAttributedString archive). The string follows the NSString class marker,
    introduced by a '+' type code and a typedstream length prefix.
    """
    if not data:
        return ""
    i = data.find(b"NSString")
    if i == -1:
        return ""
    p = data.find(b"+", i)
    if p == -1 or p + 1 >= len(data):
        return ""
    try:
        marker = data[p + 1]
        if marker == 0x81:  # 2-byte length follows
            length = int.from_bytes(data[p + 2:p + 4], "little")
            start = p + 4
        elif marker == 0x82:  # 4-byte length follows
            length = int.from_bytes(data[p + 2:p + 6], "little")
            start = p + 6
        else:  # single-byte length
            length = marker
            start = p + 2
        return data[start:start + length].decode("utf-8", "replace")
    except Exception:
        return ""


def fetch_messages(con, chat_id, limit):
    """Return up to `limit` most-recent text messages (chronological).

    Pulls extra rows because many messages are attachments/tapbacks that clean to
    empty; those are skipped so the indexed list is all real text.
    """
    cap = max(limit * 6, 240)
    rows = con.execute(
        """
        SELECT m.is_from_me, h.id, m.text, m.attributedBody, m.date
        FROM chat_message_join cmj
        JOIN message m ON m.ROWID = cmj.message_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id = ?
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (chat_id, cap),
    ).fetchall()

    out = []
    for is_me, hid, text, ab, date in rows:
        body = text if (text and text.strip()) else decode_attributed_body(ab)
        body = (body or "").replace("￼", "").strip()  # drop attachment placeholders
        if not body:
            continue
        out.append((is_me, hid, body, date))
        if len(out) >= limit:
            break
    return list(reversed(out))  # chronological


# ----------------------------- interactive helpers -----------------------------
def prompt(msg):
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit("Aborted.")


def pick_int(msg, lo, hi):
    while True:
        s = prompt(msg)
        if s.isdigit() and lo <= int(s) <= hi:
            return int(s)
        print(f"  enter a number {lo}-{hi}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_DEFAULT), help="path to chat.db")
    ap.add_argument("--show", type=int, default=40, help="recent messages to show (default 40)")
    ap.add_argument("--chats", type=int, default=25,
                    help="how many group chats to list (default 25)")
    ap.add_argument("--chat", default=None,
                    help="filter the chat list to names/participants matching this text "
                         "(reaches chats far down the recency list)")
    ap.add_argument("--dry-run", action="store_true", help="never write Contacts")
    ap.add_argument("--note", default=None,
                    help="note to add to every new contact (skips the prompt; "
                         "pass '' to force no note)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds to pause between contact creations (default 1.5). "
                         "Spacing them out avoids stuck iCloud bulk-sync; set 0 to disable.")
    args = ap.parse_args()

    if not FIRST_NAMES:
        print("Note: name dataset not found in data/ — falling back to structural "
              "name detection only. Run build_name_data.py to enable dataset matching.")
    con = connect(args.db)

    # 1. pick a group chat
    chats = list_group_chats(con, limit=(500 if args.chat else args.chats), query=args.chat)
    if not chats:
        sys.exit(f"No group chats match {args.chat!r}." if args.chat else "No group chats found.")
    header = f"Group chats matching {args.chat!r}" if args.chat else "Group chats (most recent first)"
    print(f"\n{header}:")
    for i, (cid, dname, parts) in enumerate(chats):
        label = dname or (parts[:50] + ("…" if len(parts) > 50 else ""))
        print(f"  [{i}] {label}")
    chat_idx = pick_int("Pick a chat #: ", 0, len(chats) - 1)
    chat_id = chats[chat_idx][0]

    # 2. existing contacts (also used to label known senders)
    print("\nReading your Contacts…")
    try:
        existing, contact_names = load_existing_contacts()
    except RuntimeError as e:
        sys.exit(f"Contacts access failed: {e}\nGrant Contacts access to Terminal and retry.")
    print(f"  {len(existing)} handles in your address book.")

    # 3. show recent messages, indexed
    msgs = fetch_messages(con, chat_id, args.show)
    if not msgs:
        sys.exit("No text messages in that chat.")
    print(f"\nLast {len(msgs)} messages (oldest → newest):\n")
    for i, (is_me, hid, text, _date) in enumerate(msgs):
        if is_me:
            who = "you"
        else:
            key = normalize_handle(hid)
            who = contact_names.get(key) or hid or "?"
        snippet = text.replace("\n", " ")
        snippet = snippet[:45] + ("…" if len(snippet) > 45 else "")
        print(f"  [{i:>3}] {who[:22]:<22} | {snippet}")

    # 4. pick the block
    print("\nSelect the block where people sent their names.")
    start = pick_int("Start index: ", 0, len(msgs) - 1)
    end = pick_int(f"End index ({start}-{len(msgs) - 1}): ", start, len(msgs) - 1)
    block = msgs[start:end + 1]

    # 5. best candidate name per unknown sender
    best = {}  # key -> (conf, name, raw_handle)
    seen_unknown = {}  # key -> raw_handle (any unknown sender, even w/o name)
    for is_me, hid, text, _date in block:
        if is_me or not hid:
            continue
        key = normalize_handle(hid)
        if not key or key in existing:
            continue
        seen_unknown.setdefault(key, hid)
        ok, conf, clean = name_score(text)
        if ok and conf > best.get(key, (0,))[0]:
            best[key] = (conf, clean, hid)

    creations = sorted(best.items(), key=lambda kv: -kv[1][0])
    no_name = [(k, h) for k, h in seen_unknown.items() if k not in best]

    if not creations and not no_name:
        print("\nEveryone in that block is already a contact. Nothing to do. ✅")
        return

    # 6. review loop
    selected = {k: True for k, _ in creations}
    names = {k: v[1] for k, v in creations}

    def render():
        print("\n=== Proposed new contacts ===")
        if not creations:
            print("  (none auto-detected)")
        for n, (k, (conf, _name, hid)) in enumerate(creations):
            mark = "[x]" if selected[k] else "[ ]"
            print(f"  {mark} {n}: {names[k]:<24} → {hid}   (conf {conf:.2f})")
        if no_name:
            print("\n  Unknown senders with no name-like message (not selected):")
            for k, hid in no_name:
                print(f"      - {hid}")
        print(
            "\nCommands:  ok=create selected  |  skip N  |  add N  |  edit N New Name"
            "  |  add-h N First Last  (promote a no-name sender)  |  q=quit"
        )

    noname_idx = {i: (k, h) for i, (k, h) in enumerate(no_name)}
    while True:
        render()
        cmd = prompt("> ").split()
        if not cmd:
            continue
        op = cmd[0].lower()
        if op == "q":
            print("Aborted, nothing created.")
            return
        if op == "ok":
            break
        if op in ("skip", "add") and len(cmd) == 2 and cmd[1].isdigit():
            n = int(cmd[1])
            if 0 <= n < len(creations):
                selected[creations[n][0]] = (op == "add")
            continue
        if op == "edit" and len(cmd) >= 3 and cmd[1].isdigit():
            n = int(cmd[1])
            if 0 <= n < len(creations):
                names[creations[n][0]] = " ".join(cmd[2:])
            continue
        if op == "add-h" and len(cmd) >= 3 and cmd[1].isdigit():
            n = int(cmd[1])
            if n in noname_idx:
                k, hid = noname_idx[n]
                conf = 1.0
                names[k] = " ".join(cmd[2:])
                selected[k] = True
                creations.append((k, (conf, names[k], hid)))
            continue
        print("  ?")

    to_create = [(k, names[k], dict(creations)[k][2]) for k, _ in creations if selected.get(k)]
    if not to_create:
        print("Nothing selected. Done.")
        return

    # 7. optional note applied to every new contact
    note = args.note
    if note is None:
        note = prompt(f"\nOptional note to add to all {len(to_create)} contacts "
                      "(Enter to skip): ").strip()

    # 8. create
    print()
    if args.dry_run:
        for _k, name, hid in to_create:
            print(f"  [dry-run] would create: {name} → {hid}")
        if note:
            print(f"  [dry-run] note on all: {note!r}")
        print(f"\nDry run: {len(to_create)} contact(s) not written.")
        return

    ok_n = 0
    # Space creations apart: rapid bulk creation can stall iCloud (CardDAV) sync so
    # contacts never reach the phone. One push at a time syncs reliably.
    for i, (_k, name, hid) in enumerate(to_create):
        try:
            create_contact(name, hid, note)
            print(f"  ✅ {name} → {hid}")
            ok_n += 1
        except RuntimeError as e:
            print(f"  ❌ {name} → {hid}: {e}")
        if args.delay > 0 and i < len(to_create) - 1:
            time.sleep(args.delay)
    suffix = f" with note {note!r}" if note else ""
    print(f"\nCreated {ok_n}/{len(to_create)} contact(s){suffix}.")


if __name__ == "__main__":
    main()
