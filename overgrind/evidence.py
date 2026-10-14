"""DIAG: failure classification, fingerprinting and errors-only atomic evidence store.

Pure stdlib. No global state. No file is ever written for a successful case.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .types import (
    Action, Case, ErrorEvent, Health, HarnessError, Outcome, TargetFailure,
)

REDACTED = "***"
CATEGORIES = ("crash", "hang", "dialog", "assertion", "resource", "harness")


# ------------------------------------------------------------------ ring buffer
class RingBuffer:
    """Bounded last-N action buffer."""

    def __init__(self, n: int = 200) -> None:
        self._d: deque[Action] = deque(maxlen=max(1, int(n)))

    def push(self, action: Action) -> None:
        self._d.append(action)

    def snapshot(self) -> list[Action]:
        return list(self._d)

    def clear(self) -> None:
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


# ------------------------------------------------------------------ classification
def classify(obj: Any, message: str = "") -> Outcome:
    """Map an exception or a Health snapshot (or pass an Outcome through) to an Outcome.

    HarnessError (and any unknown exception) -> category 'harness': never an app defect.
    """
    if isinstance(obj, Outcome):
        return obj
    if isinstance(obj, Health):
        st = obj.status
        msg = message or obj.detail
        if st == "ok":
            return Outcome(ok=True)
        cat = {"dead": "crash", "hung": "hang", "modal": "dialog"}.get(st)
        if cat is None:  # 'missing' or unknown: our driver lost the target, not a defect
            return Outcome(False, "harness", msg or f"target {st}", {"health": st})
        return Outcome(False, cat, msg, {"health": st})
    if isinstance(obj, TargetFailure):
        cat = obj.category if obj.category in CATEGORIES else "crash"
        return Outcome(False, cat, message or str(obj), {"exc": "TargetFailure"})
    if isinstance(obj, HarnessError):
        return Outcome(False, "harness", message or str(obj), {"exc": "HarnessError"})
    if isinstance(obj, BaseException):
        return Outcome(False, "harness", message or f"{type(obj).__name__}: {obj}",
                       {"exc": type(obj).__name__})
    if obj is None:
        return Outcome(ok=True)
    raise TypeError(f"cannot classify {type(obj).__name__}")


def is_app_defect(outcome: Outcome) -> bool:
    return (not outcome.ok) and outcome.category != "harness"


# ------------------------------------------------------------------ fingerprint
_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<ts>"),
    (re.compile(r"(?i)\b(?:pid|tid|process id|thread id)\s*[=:#]?\s*\d+"), "pid <n>"),
    (re.compile(r"(?i)\b[a-z]:\\(?:[^\\/:*?\"<>|\s]+\\)+"), "<dir>/"),      # windows dir part
    (re.compile(r"(?<![\w.])/(?:[\w.\-~+@%]+/)+"), "<dir>/"),                # posix dir part
    (re.compile(r"(?i)\b0x[0-9a-f]+\b"), "<hex>"),
    (re.compile(r"(?i)\b[0-9a-f]{8,}\b"), "<hex>"),
    (re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?"), "N"),
)


def normalize_text(text: str) -> str:
    s = str(text or "")
    for rx, rep in _PATTERNS:
        s = rx.sub(rep, s)
    return re.sub(r"\s+", " ", s).strip().lower()


def fingerprint(category: str, message: str = "", hint: str = "", stack: Any = "",
                module: str = "", exc_type: str = "") -> str:
    """Short stable hex signature (16 chars).

    Volatile text (pids, addresses, timestamps, temp paths, numbers) is normalised, so equal
    faults dedup. Crash module / exception type / stack frames are kept verbatim (normalised
    only for volatile bits), so different faults do not collapse.
    """
    if isinstance(stack, (list, tuple)):
        stack = "|".join(str(x) for x in stack)
    parts = [
        str(category or "").lower(),
        normalize_text(exc_type),
        normalize_text(module),
        normalize_text(stack),
        normalize_text(hint),
        normalize_text(message),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def outcome_fingerprint(outcome: Outcome, hint: str = "") -> str:
    d = outcome.detail or {}
    return fingerprint(outcome.category, outcome.message, hint or str(d.get("hint", "")),
                       d.get("stack", ""), str(d.get("module", "")), str(d.get("exc_type", "")))


# ------------------------------------------------------------------ env capture
def capture_env(exe_path: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    env: dict[str, Any] = {
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
    }
    if exe_path:
        env["exe_path"] = exe_path
        try:
            h = hashlib.sha256()
            with open(exe_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            env["exe_sha256"] = h.hexdigest()
        except OSError:
            env["exe_sha256"] = ""
    if extra:
        env.update(extra)
    return env


# ------------------------------------------------------------------ redaction
_SECRET_NAME = re.compile(r"(?i)pass(word|wd)?|pwd|^pw$|secret|token|api[_-]?key")
_SECRET_TEXT = re.compile(
    r"(?i)\b(pass(?:word|wd)?|pwd|secret|token|api[_-]?key)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")


def redact_text(text: str) -> str:
    return _SECRET_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


MIN_SCRUB_LEN = 4        # shorter secrets would mangle ordinary text when scrubbed from free-form fields


def scrub_literals(text: str, secrets: Iterable[str]) -> str:
    """Replace every literal occurrence of a known sensitive value (longest first)."""
    for sv in sorted((x for x in set(secrets) if isinstance(x, str) and len(x) >= MIN_SCRUB_LEN),
                     key=len, reverse=True):
        if sv in text:
            text = text.replace(sv, REDACTED)
    return text


def _redact_obj(o: Any, secrets: Iterable[str] = ()) -> Any:
    if isinstance(o, str):
        return scrub_literals(redact_text(o), secrets)
    if isinstance(o, dict):
        return {k: (REDACTED if isinstance(k, str) and _SECRET_NAME.search(k) and o[k] not in (None, "")
                    else _redact_obj(v, secrets)) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_redact_obj(x, secrets) for x in o]
    return o


def redact_event(event: ErrorEvent, sensitive: Iterable[str] = ()) -> ErrorEvent:
    """Return a redacted deep copy; the input is untouched.

    A control is hidden when its id is in ``sensitive`` or looks like a secret (password/token/...).
    Its values are replaced by ``***`` in the case and the action history, and every literal copy of
    those values is scrubbed from the message, env and the other free-form fields (an app that echoes
    its input in the crash text must not leak it through there)."""
    sens = set(sensitive)
    ev = copy.deepcopy(event)

    def hidden(name: str) -> bool:
        return name in sens or bool(_SECRET_NAME.search(name or ""))

    secrets = {v for k, v in ev.case.values.items() if hidden(k) and isinstance(v, str)}
    secrets |= {a.value for a in ev.history if a.kind == "control" and hidden(a.target) and isinstance(a.value, str)}
    ev.case = Case(
        seq=ev.case.seq, state=dict(ev.case.state),
        values={k: (REDACTED if hidden(k) and v not in (None, "") else _redact_obj(v, secrets))
                for k, v in ev.case.values.items()},
        order=tuple(ev.case.order), dwell_ms=ev.case.dwell_ms, tags=tuple(ev.case.tags))
    ev.history = [
        Action(a.kind, a.target,
               REDACTED if (hidden(a.target) and a.kind == "control" and a.value not in (None, ""))
               else _redact_obj(a.value, secrets), a.t_ms, a.ok)
        for a in ev.history]
    ev.message = scrub_literals(redact_text(ev.message), secrets)
    ev.env = _redact_obj(ev.env, secrets)
    if ev.reduced is not None:
        ev.reduced = redact_reduced(ev.reduced, sens, secrets)
    return ev


def redact_reduced(reduced: dict[str, Any], sensitive: Iterable[str] = (),
                   secrets: Iterable[str] = ()) -> dict[str, Any]:
    sens = set(sensitive)
    r = copy.deepcopy(reduced)

    def hid(t: str) -> bool:
        return t in sens or bool(_SECRET_NAME.search(t))

    sec = set(secrets)
    for st in r.get("steps", []):
        if st.get("kind") == "control" and hid(str(st.get("target", ""))) and isinstance(st.get("value"), str):
            sec.add(st["value"])
    if isinstance(r.get("values"), dict):
        sec |= {v for k, v in r["values"].items() if hid(k) and isinstance(v, str)}
    for st in r.get("steps", []):
        t = str(st.get("target", ""))
        if st.get("kind") == "control" and hid(t) and st.get("value") not in (None, ""):
            st["value"] = REDACTED
        elif isinstance(st.get("value"), str):
            st["value"] = scrub_literals(st["value"], sec)
    if isinstance(r.get("values"), dict):
        for k in list(r["values"]):
            if hid(k) and r["values"][k] not in (None, ""):
                r["values"][k] = REDACTED
            else:
                r["values"][k] = _redact_obj(r["values"][k], sec)
    for key in ("message", "env"):
        if key in r:
            r[key] = _redact_obj(r[key], sec)
    return r


# ------------------------------------------------------------------ (de)serialisation
def event_to_dict(event: ErrorEvent) -> dict[str, Any]:
    return {
        "id": event.id, "ts": event.ts, "run_id": event.run_id, "category": event.category,
        "signature": event.signature, "message": event.message,
        "case": {"seq": event.case.seq, "state": dict(event.case.state),
                 "values": dict(event.case.values), "order": list(event.case.order),
                 "dwell_ms": event.case.dwell_ms, "tags": list(event.case.tags)},
        "history": [asdict(a) for a in event.history],
        "env": event.env, "attachments": dict(event.attachments), "count": event.count,
        "reliability": event.reliability, "reduced": event.reduced,
    }


def event_from_dict(d: dict[str, Any]) -> ErrorEvent:
    c = d["case"]
    case = Case(seq=c["seq"], state=dict(c.get("state", {})), values=dict(c.get("values", {})),
                order=tuple(c.get("order", ())), dwell_ms=c.get("dwell_ms", 0),
                tags=tuple(c.get("tags", ())))
    return ErrorEvent(
        id=d["id"], ts=d["ts"], run_id=d["run_id"], category=d["category"],
        signature=d["signature"], message=d["message"], case=case,
        history=[Action(a["kind"], a["target"], a.get("value"), a.get("t_ms", 0.0), a.get("ok", True))
                 for a in d.get("history", [])],
        env=dict(d.get("env", {})), attachments=dict(d.get("attachments", {})),
        count=d.get("count", 1), reliability=d.get("reliability", "unconfirmed"),
        reduced=d.get("reduced"))


def replace_with_retry(src: Any, dst: Any, attempts: int = 8) -> None:
    """os.replace that rides out Windows sharing violations (the GUI or an AV scanner has the file open)."""
    delay = 0.01
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def atomic_write_text(path: Path, text: str) -> None:
    """temp file in the same dir + fsync + os.replace. A crash leaves the old file intact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _safe(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(name))
    return s.strip(".") or "_"


# ------------------------------------------------------------------ store
class EvidenceStore:
    """dir/errors/<signature>/<id>.json (+ <id>/ attachments dir), dir/index.json.

    Repeats of a known signature only bump `count`; at most `max_incidents` full incidents are
    kept per signature. Nothing is written for successes (there is no API for it).
    """

    def __init__(self, directory: str | os.PathLike, *, sensitive: Iterable[str] = (),
                 max_incidents: int = 5, max_bytes: int = 500 * 1024 * 1024) -> None:
        self.dir = Path(directory)
        self.sensitive = frozenset(sensitive)
        self.max_incidents = max(1, int(max_incidents))
        self.max_bytes = int(max_bytes)

    # -- paths / index
    @property
    def index_path(self) -> Path:
        return self.dir / "index.json"

    def _read_index(self) -> dict[str, Any]:
        try:
            idx = json.loads(self.index_path.read_text(encoding="utf-8"))
            return idx if isinstance(idx, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_index(self, idx: dict[str, Any]) -> None:
        atomic_write_text(self.index_path, json.dumps(idx, indent=1, sort_keys=True))

    def incident_path(self, signature: str, event_id: str) -> Path:
        return self.dir / "errors" / _safe(signature) / (_safe(event_id) + ".json")

    def attachments_dir(self, signature: str, event_id: str) -> Path:
        return self.dir / "errors" / _safe(signature) / _safe(event_id)

    def _has_incidents(self, signature: str) -> bool:
        d = self.dir / "errors" / _safe(signature)
        try:
            return d.is_dir() and any(d.glob("*.json"))
        except OSError:
            return False

    def known(self, signature: str) -> bool:
        """True if the signature was seen: in the index, or (after a crash between the incident file
        and the index write) as an incident file on disk."""
        return signature in self._read_index() or self._has_incidents(signature)

    def counts(self) -> dict[str, int]:
        return {s: int(e.get("count", 1)) for s, e in self._read_index().items()}

    # -- write
    def write(self, event: ErrorEvent, attachment_files: dict[str, str] | None = None) -> str:
        """Persist an incident (or just bump the count of a known signature that is full).

        Returns the path of the incident file written, or of the most recent stored incident
        for the signature when only the count was bumped. Harness errors are refused.
        """
        if event.category == "harness":
            raise ValueError("harness errors are not app defects and are not stored as errors")
        ev = redact_event(event, self.sensitive)          # everything below (index included) sees only this copy
        idx = self._read_index()
        ent = idx.get(event.signature)
        if ent is None:
            ent = self._recover_entry(event.signature, ev)
            idx[event.signature] = ent
        ent["count"] = int(ent.get("count", 0)) + 1
        ent["last_ts"] = event.ts
        if len(ent["ids"]) >= self.max_incidents:
            self._write_index(idx)
            return str(self.incident_path(event.signature, ent["ids"][-1]))

        ev.count = 1
        adir = self.attachments_dir(ev.signature, ev.id)
        if attachment_files:
            adir.mkdir(parents=True, exist_ok=True)
            for name, src in attachment_files.items():
                if not src or not os.path.isfile(src):
                    continue
                dst = adir / _safe(os.path.basename(src) if not name else name + Path(src).suffix)
                shutil.copyfile(src, dst)
                ev.attachments[name] = dst.relative_to(self.dir).as_posix()
        path = self.incident_path(ev.signature, ev.id)
        atomic_write_text(path, json.dumps(event_to_dict(ev), indent=1, default=str))
        ent["ids"].append(_safe(ev.id))
        self._write_index(idx)
        self.enforce_retention()
        return str(path)

    def _recover_entry(self, signature: str, event: ErrorEvent | None = None) -> dict[str, Any]:
        """Index entry for a signature (rebuilt from incident files if the index lost it)."""
        ids: list[str] = []
        d = self.dir / "errors" / _safe(signature)
        try:
            ids = sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []
        except OSError:
            ids = []
        ts = event.ts if event is not None else ""
        return {"count": len(ids), "category": event.category if event else "", "ids": ids,
                "message": (event.message[:200] if event else ""), "first_ts": ts, "last_ts": ts}

    def bump(self, signature: str, n: int = 1) -> int:
        idx = self._read_index()
        if signature not in idx:
            if not self._has_incidents(signature):
                raise KeyError(signature)
            idx[signature] = self._recover_entry(signature)
        idx[signature]["count"] = int(idx[signature].get("count", 0)) + n
        self._write_index(idx)
        return idx[signature]["count"]

    # -- read
    def _load_file(self, path: Path, idx: dict[str, Any]) -> ErrorEvent | None:
        try:
            ev = event_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None
        ent = idx.get(ev.signature)
        if ent:
            ev.count = max(ev.count, int(ent.get("count", 1)))
        ev.attachments = {k: v for k, v in ev.attachments.items()
                          if os.path.isabs(v) or (self.dir / v).exists()}
        red = path.with_suffix("") / "reduced.json"
        if red.is_file():
            try:
                ev.reduced = json.loads(red.read_text(encoding="utf-8"))
                ev.reliability = ev.reduced.get("reliability", ev.reliability)
            except (OSError, ValueError):
                pass
        return ev

    def list(self) -> list[ErrorEvent]:
        """Oldest first. Source of truth is the incident files (tolerates a lost index)."""
        idx = self._read_index()
        out: list[ErrorEvent] = []
        root = self.dir / "errors"
        if not root.is_dir():
            return out
        for p in sorted(root.glob("*/*.json")):
            ev = self._load_file(p, idx)
            if ev is not None:
                out.append(ev)
        out.sort(key=lambda e: (e.ts, e.id))
        return out

    def find_path(self, event_id: str) -> Path | None:
        root = self.dir / "errors"
        for p in root.glob(f"*/{_safe(event_id)}.json") if root.is_dir() else ():
            return p
        return None

    def load(self, event_id: str) -> ErrorEvent:
        p = self.find_path(event_id)
        if p is None:
            raise KeyError(event_id)
        ev = self._load_file(p, self._read_index())
        if ev is None:
            raise KeyError(event_id)
        return ev

    def write_reduced(self, event: ErrorEvent, reduced: dict[str, Any]) -> str:
        """Write <id>/reduced.json next to the (untouched) incident file."""
        d = self.attachments_dir(event.signature, event.id)
        path = d / "reduced.json"
        atomic_write_text(path, json.dumps(redact_reduced(reduced, self.sensitive), indent=1, default=str))
        return str(path)

    # -- retention
    def total_bytes(self) -> int:
        n = 0
        for root, _d, files in os.walk(self.dir):
            for f in files:
                try:
                    n += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return n

    def enforce_retention(self) -> int:
        """Delete oldest attachments first, then oldest incidents, until under the cap."""
        total = self.total_bytes()
        if total <= self.max_bytes:
            return 0
        removed = 0
        errs = self.dir / "errors"

        def files(attachments: bool) -> list[tuple[float, Path]]:
            res = []
            it = errs.glob("*/*/*") if attachments else errs.glob("*/*.json")
            for p in it:
                if p.is_file() and not (attachments and p.name == "reduced.json"):
                    try:
                        res.append((p.stat().st_mtime, p))
                    except OSError:
                        pass
            return sorted(res)

        for group in (True, False):
            for _m, p in files(group):
                if total <= self.max_bytes:
                    return removed
                try:
                    sz = p.stat().st_size
                    p.unlink()
                except OSError:
                    continue
                total -= sz
                removed += 1
        return removed
