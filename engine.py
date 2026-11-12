"""Game engine: state machine, outbox, clue logic. No network code in here."""
import datetime as dt
import json
import re
import sqlite3
import threading
import unicodedata
from zoneinfo import ZoneInfo

import yaml

SUBJECT = "Adda's birthday treasure hunt"
DEFAULT_FOOT = "Reply with your answer, or say CLUE for a hint."
SIGNATURE = "-- The Game Master"

CLUE_RE = re.compile(
    r"\b(clues?|hints?|help|stuck|give up|no idea|dunno|don t know|dont know|"
    r"indiciu|indicii|ajutor|ajutati|nu stiu|nu imi dau seama)\b"
)
STREET_RE = re.compile(
    r"\b(str|strada|bd|bld|bulevardul|bulevard|aleea|calea|sos|soseaua|nr|"
    r"street|road|avenue|blvd)\b"
)
OPEN_RE = re.compile(r"\b(begin|start|games?)\b")


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


class Hunt:
    def __init__(self, path):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.raw = raw
        self.tz = ZoneInfo(raw.get("timezone", "Europe/Bucharest"))
        self.live_date = dt.date.fromisoformat(str(raw["live_date"]))
        self.stages = raw["stages"]
        for s in self.stages:
            s.setdefault("kind", "riddle")
            s.setdefault("answers", [])
            s.setdefault("clues", [])
            s.setdefault("reveal", "")
        self.index = {s["id"]: i for i, s in enumerate(self.stages)}
        self.addr_idx = next(i for i, s in enumerate(self.stages) if s["kind"] == "address")
        nd = raw.get("nudge", {})
        self.offer_min = nd.get("offer_after_min", 30)
        self.clue_min = nd.get("clue_after_min", 60)
        self.repeat_min = nd.get("repeat_every_min", 40)

    def at(self, hhmm):
        h, m = [int(x) for x in str(hhmm).split(":")]
        return dt.datetime.combine(self.live_date, dt.time(h, m), tzinfo=self.tz)


def fallback_hint(st, k):
    """Deterministic last resort clue when the LLM is unavailable."""
    ans = (st.get("answers") or [""])[0]
    if not ans:
        return "Just answer the riddle as best you can, any guess is fine."
    letters = [c for c in ans if c.isalpha()]
    if k <= 0:
        return f"The answer has {len(letters)} letters and starts with {ans[0].upper()}."
    shown = min(k + 1, max(1, len(ans) - 1))
    if k >= 4:
        return f"The answer is: {ans}."
    return f"The answer starts with '{ans[:shown].upper()}' and has {len(letters)} letters."


class Game:
    def __init__(self, db_path, hunt, llm=None, mailer=None, now_fn=None, cfg=None):
        self.hunt = hunt
        self.llm = llm
        self.mailer = mailer
        self.now_fn = now_fn or (lambda: dt.datetime.now(hunt.tz))
        self.cfg = cfg or {}
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript(
            """
            create table if not exists kv(k text primary key, v text);
            create table if not exists seen(msgid text primary key);
            create table if not exists log(id integer primary key, ts text, kind text, text text);
            create table if not exists outbox(
              id integer primary key, send_after text, to_addr text, subject text,
              body text, kind text, stage_id text, sent_at text, attempts integer default 0);
            """
        )
        self.state = self._load()

    # ------------------------------------------------------------ persistence
    def _default_state(self):
        return {
            "mode": (self.cfg.get("mode") or "TEST").upper(),
            "started": False, "finished": False, "paused": False, "bundled": False,
            "idx": -1, "clue_n": {}, "generated": {}, "sent": {}, "sent_at": {},
            "idle_since": None, "nudge_n": 0, "last_nudge_at": None,
            "last_inbound_msgid": None, "solves": [],
        }

    def _load(self):
        row = self.db.execute("select v from kv where k='state'").fetchone()
        st = self._default_state()
        if row:
            st.update(json.loads(row[0]))
        return st

    def save(self):
        self.db.execute("insert or replace into kv values('state', ?)", (json.dumps(self.state),))
        self.db.commit()

    def log(self, kind, text):
        self.db.execute("insert into log(ts, kind, text) values(?,?,?)",
                        (self.now().isoformat(timespec="seconds"), kind, text[:2000]))
        self.db.commit()

    # ------------------------------------------------------------- helpers
    def now(self):
        return self.now_fn()

    @property
    def live(self):
        return self.state["mode"] == "LIVE"

    def player(self):
        return self.cfg.get("player_live" if self.live else "player_test", "")

    def admin(self):
        return self.cfg.get("admin_email", "")

    def stage(self):
        i = self.state["idx"]
        return self.hunt.stages[i] if 0 <= i < len(self.hunt.stages) else None

    def _t(self, s):
        return dt.datetime.fromisoformat(s) if s else None

    def _shift(self, t):
        if not self.live:
            return t
        bs, be = self.hunt.at(self.hunt.raw["blackout"]["start"]), self.hunt.at(self.hunt.raw["blackout"]["end"])
        return be if bs <= t < be else t

    def in_blackout(self):
        return self.live and self._shift(self.now()) != self.now()

    def _enqueue(self, to, body, kind, stage_id="", when=None):
        when = when or self.now()
        if to == self.player() and to != self.admin():
            when = self._shift(max(when, self.now()))
        prefix = "" if (self.live or to != self.player()) else "[TEST] "
        subject = prefix + SUBJECT if to == self.player() else f"{prefix}Hunt status"
        if to == self.player():
            body = body.rstrip() + "\n\n" + SIGNATURE
        self.db.execute(
            "insert into outbox(send_after,to_addr,subject,body,kind,stage_id) values(?,?,?,?,?,?)",
            (when.isoformat(), to, subject, body, kind, stage_id))
        self.db.commit()

    def _status(self, text):
        if self.live and self.admin():
            self._enqueue(self.admin(), text, "status")

    def riddle_text(self, st):
        parts = []
        if st.get("header"):
            parts.append(st["header"])
        parts.append(st["riddle"].strip())
        foot = st.get("footer", DEFAULT_FOOT)
        if foot:
            parts.append(foot)
        return "\n\n".join(parts)

    # ------------------------------------------------------------- control
    def send_opening(self):
        with self.lock:
            if self.state["started"]:
                return False
            st = self.hunt.stages[0]
            self.state.update(started=True, idx=0)
            self.state["sent"][st["id"]] = False
            self._enqueue(self.player(), self.riddle_text(st), "riddle", st["id"])
            self.state["idle_since"] = self.now().isoformat()
            self.state["nudge_n"] = 0
            self.log("opening", "queued opening")
            self.save()
            return True

    def reset(self):
        with self.lock:
            self.db.execute("delete from outbox")
            self.db.execute("delete from seen")
            mode = self.state["mode"]
            self.state = self._default_state()
            self.state["mode"] = mode
            self.log("admin", "reset")
            self.save()

    def set_mode(self, mode):
        with self.lock:
            self.state["mode"] = mode.upper()
            self.log("admin", f"mode {mode}")
            self.save()

    def jump(self, stage_id):
        with self.lock:
            i = self.hunt.index[stage_id]
            st = self.hunt.stages[i]
            self.state.update(idx=i, started=True)
            self.state["sent"][stage_id] = False
            self._enqueue(self.player(), self.riddle_text(st), "riddle", stage_id)
            self.state["idle_since"] = self.now().isoformat()
            self.state["nudge_n"] = 0
            self.log("admin", f"jump {stage_id}")
            self.save()

    # ------------------------------------------------------------- inbound
    def handle_inbound(self, text, msgid=None):
        with self.lock:
            if msgid:
                if self.db.execute("select 1 from seen where msgid=?", (msgid,)).fetchone():
                    return "duplicate"
                self.db.execute("insert into seen values(?)", (msgid,))
                self.state["last_inbound_msgid"] = msgid
            self.log("inbound", text)
            if self.state["paused"] or not self.state["started"]:
                self.save()
                return "ignored"
            if self.state["finished"]:
                self._enqueue(self.player(), "The hunt is over and you won. Go and enjoy your birthday.", "info")
                self.save()
                return "finished"
            st = self.stage()
            self.state["idle_since"] = self.now().isoformat()
            self.state["nudge_n"] = 0
            label = self._classify(st, text)
            self.log("label", f"{st['id']}: {label}")
            if label == "correct":
                self._advance()
            elif label == "clue":
                self._clue(st, "asked")
            elif label == "chitchat":
                self._enqueue(self.player(), "Nice try, but the game goes on. Answer the riddle, or say CLUE.", "info", st["id"])
            else:
                self._clue(st, "wrong")
            self.save()
            return label

    def _match(self, st, n):
        words = n.split()
        for a in st.get("answers", []):
            an = norm(a)
            if not an or (len(an) <= 3 and len(words) > 4):
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(an)}(?![a-z0-9])", n):
                return True
        if st["kind"] == "address":
            if (re.search(r"\d", n) and len(words) >= 2) or STREET_RE.search(n):
                return True
        return False

    def _classify(self, st, text):
        n = norm(text)
        if not n:
            return "chitchat"
        clue = bool(CLUE_RE.search(n))
        kind = st["kind"]
        if kind == "lunch":
            return "clue" if clue and len(n.split()) <= 4 else "correct"
        if kind == "opening":
            if OPEN_RE.search(n):
                return "correct"
            return "clue" if clue else "wrong"
        if self._match(st, n):
            return "correct"
        if clue:
            return "clue"
        if self.llm:
            label = self.llm.classify(st, text)
            if label in ("correct", "clue", "wrong", "chitchat"):
                return label
        return "wrong"

    # ------------------------------------------------------------- clues
    def _generate(self, st, n):
        bank = st["clues"]
        gen = self.state["generated"].setdefault(st["id"], [])
        k = n - len(bank)
        text = None
        if self.llm:
            for _ in range(2):
                cand = self.llm.make_clue(st, bank + gen, n + 1)
                if cand and (k >= 4 or not self._leaks(st, cand)):
                    text = cand
                    break
        if not text:
            text = fallback_hint(st, k)
        gen.append(text)
        return text

    def _leaks(self, st, text):
        return self._match({**st, "kind": "riddle"}, norm(text))

    def _clue(self, st, reason, nudge=False):
        sid = st["id"]
        n = self.state["clue_n"].get(sid, 0)
        text = st["clues"][n] if n < len(st["clues"]) else self._generate(st, n)
        self.state["clue_n"][sid] = n + 1
        self.log("clue", f"{sid} #{n + 1} ({reason}): {text}")
        body = f"Clue {n + 1}\n\n{text}\n\n{DEFAULT_FOOT}"
        self._enqueue(self.player(), body, "clue", sid)

    # ------------------------------------------------------------- advance
    def _advance(self, note=""):
        s = self.state
        now = self.now()
        i = s["idx"]
        st = self.hunt.stages[i]
        sent_at = self._t(s["sent_at"].get(st["id"]))
        if sent_at:
            s["solves"].append([st["id"], int((now - sent_at).total_seconds())])
        reveal = " ".join(x for x in [st["reveal"].strip(), note] if x)
        if st.get("final"):
            self._enqueue(self.player(), reveal, "reveal", st["id"])
            s["finished"] = True
            self._status(f"Hunt finished at {now:%H:%M}. She solved the last riddle.")
            self.log("finish", "finished")
            return
        nxt = self.hunt.stages[i + 1]
        gate = self.hunt.at(nxt["gate"]) if (self.live and nxt.get("gate")) else None
        when = max(now, gate) if gate else now
        if self.live:
            when = self._shift(when)
        riddle = self.riddle_text(nxt)
        if when <= now and reveal:
            self._enqueue(self.player(), reveal + "\n\n---\n\n" + riddle, "riddle", nxt["id"], now)
        else:
            if reveal:
                if when > now:
                    reveal += f"\n\nThe next riddle arrives at {when:%H:%M}."
                self._enqueue(self.player(), reveal, "reveal", st["id"], now)
            self._enqueue(self.player(), riddle, "riddle", nxt["id"], when)
        s["idx"] = i + 1
        s["sent"][nxt["id"]] = False
        s["idle_since"] = now.isoformat()
        s["nudge_n"] = 0
        s["last_nudge_at"] = None
        took = f"{s['solves'][-1][1] // 60} min" if sent_at else "n/a"
        self._status(f"Solved '{st['id']}' at {now:%H:%M} (took {took}). Next: '{nxt['id']}', sending at {when:%H:%M}.")
        self.log("advance", f"{st['id']} -> {nxt['id']}")

    # ------------------------------------------------------------- tick
    def tick(self):
        with self.lock:
            if not self.state["paused"]:
                if self.live:
                    self._live_rules()
            self._flush()
            self.save()

    def _live_rules(self):
        s, h, now = self.state, self.hunt, self.now()
        if not s["started"]:
            if now >= h.at(h.raw["opening_time"]):
                self.send_opening()
            return
        if s["finished"]:
            return
        if now >= h.at(h.raw["dinner_deadline"]):
            self._enqueue(self.player(), h.raw["finale_forced"], "reveal")
            s["finished"] = True
            self._status("Dinner deadline reached, restaurant revealed.")
            self.log("finish", "dinner deadline")
            return
        if now >= h.at(h.raw["bundle_at"]) and not s["bundled"]:
            s["bundled"] = True
            if 1 <= s["idx"] < h.addr_idx:
                gifts = [x["reveal"].strip() for x in h.stages[s["idx"]:h.addr_idx] if x["reveal"].strip()]
                nxt = h.stages[h.addr_idx]
                gate = h.at(nxt["gate"]) if nxt.get("gate") else now
                when = self._shift(max(now, gate))
                self._enqueue(self.player(), "Time is running short, so here are all the gifts still waiting for you:\n\n"
                              + "\n".join(gifts) + (f"\n\nThe final stretch starts at {when:%H:%M}." if when > now else ""), "reveal")
                self._enqueue(self.player(), self.riddle_text(nxt), "riddle", nxt["id"], when)
                s["idx"] = h.addr_idx
                s["sent"][nxt["id"]] = False
                s["idle_since"] = now.isoformat()
                s["nudge_n"] = 0
                self._status("19:30 bundle: remaining gifts revealed, jumped to the address stage.")
                return
        st = self.stage()
        if not st or not s["sent"].get(st["id"]):
            return
        if st.get("deadline") and now >= h.at(st["deadline"]) and not st.get("final"):
            self.log("deadline", st["id"])
            self._advance(note=st.get("deadline_note", ""))
            return
        if self.in_blackout():
            return
        self._nudge(st, now)

    def _nudge(self, st, now):
        s, h = self.state, self.hunt
        idle = self._t(s["idle_since"]) or now
        last = self._t(s["last_nudge_at"])
        ref = max(x for x in [idle, last] if x)
        fast = st.get("fast_clue_from")
        due, give = False, True
        if fast and now >= h.at(fast):
            due = now >= ref + dt.timedelta(minutes=st.get("fast_clue_every_min", 10))
        else:
            n = s["nudge_n"]
            if n == 0:
                due, give = now >= idle + dt.timedelta(minutes=h.offer_min), False
            elif n == 1:
                due = now >= idle + dt.timedelta(minutes=h.clue_min)
            else:
                due = now >= (last or idle) + dt.timedelta(minutes=h.repeat_min)
        if not due:
            return
        if give:
            self._clue(st, "nudge", nudge=True)
        else:
            self._enqueue(self.player(), "Still with me? Reply CLUE if you would like a hint.", "nudge", st["id"])
        s["nudge_n"] += 1
        s["last_nudge_at"] = now.isoformat()

    # ------------------------------------------------------------- outbox
    def _flush(self):
        now = self.now()
        rows = self.db.execute(
            "select id,send_after,to_addr,subject,body,kind,stage_id,attempts from outbox "
            "where sent_at is null order by id").fetchall()
        for (oid, after, to, subject, body, kind, sid, attempts) in rows:
            if dt.datetime.fromisoformat(after) > now:
                continue
            if not self.mailer:
                break
            try:
                self.mailer.send(to, subject, body, self.state.get("last_inbound_msgid"))
            except Exception as e:  # retry next tick
                self.db.execute("update outbox set attempts=attempts+1 where id=?", (oid,))
                self.db.commit()
                self.log("send_error", f"{oid}: {e}")
                break
            self.db.execute("update outbox set sent_at=? where id=?", (now.isoformat(), oid))
            self.db.commit()
            if to == self.player() and sid:
                if kind == "riddle":
                    self.state["sent"][sid] = True
                    self.state["sent_at"][sid] = now.isoformat()
                    self.state["idle_since"] = now.isoformat()
                    self.state["nudge_n"] = 0
                    self.state["last_nudge_at"] = None
            self.log("sent", f"{kind} to {to}: {body[:80]!r}")
