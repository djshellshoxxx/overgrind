"""Danger guard (spec U2 3.3): a keyword classifier for UI element names that look destructive.

``classify(text) -> (bool, reason)`` answers "would clicking/toggling this probably destroy data, spend
money, send something or end the session?". It is a HEURISTIC with false negatives (other languages,
icon-only buttons, custom labels): the project editor and preflight say so, and a disposable VM or
profile is always the real protection.

Matching rules (so that "Savannah" is not "save"):
* the text is NFKC-normalised, case-folded and split into word tokens: accelerators (``&Save``), trailing
  ``...``/``…``, punctuation, snake_case, kebab-case and camelCase (``btnSaveAs`` -> btn save as) all
  separate words;
* a lexicon entry matches whole tokens (one or several in a row) or a regular inflection of its last
  token (save/saves/saved/saving); never a substring of a longer word;
* CJK entries have no word boundaries and match as substrings.

Tiers: ``block`` (never automate unless the user clears ``FeatureDef.dangerous``), ``disruptive``
(ends or changes the target session) and ``warn`` (needs a human glance).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .types import FeatureDef, Project

BLOCK = ("save", "save as", "delete", "remove", "erase", "wipe", "format", "purge", "overwrite",
         "replace all", "send", "submit", "post", "publish", "pay", "purchase", "buy", "order",
         "checkout", "check out", "transfer", "withdraw", "uninstall", "install", "upload", "share",
         "email", "sign", "confirm payment", "empty", "factory reset", "clear all", "revoke",
         "terminate", "kill", "drop",
         # de / fr / es (unreviewed seeds, extend per project)
         "speichern", "loeschen", "löschen", "senden", "bezahlen", "kaufen",
         "enregistrer", "supprimer", "envoyer", "payer",
         "guardar", "eliminar", "enviar", "pagar")
DISRUPTIVE = ("exit", "quit", "close", "shutdown", "shut down", "restart", "log off", "logoff",
              "sign out", "signout", "beenden", "quitter", "salir")
WARN = ("reset",)
CJK = {"block": ("保存", "削除", "送信", "支払", "删除", "发送", "支付")}

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NONWORD = re.compile(r"[^\w]+|_+", re.UNICODE)


@dataclass(frozen=True)
class Finding:
    tier: str
    word: str
    text: str

    def reason(self) -> str:
        return f"{self.tier}: looks like '{self.word}' ({self.text!r})"


def _tokens(text: str) -> list[str]:
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = _CAMEL.sub(" ", s)                       # btnSaveAs -> btn Save As (before case-folding!)
    s = _NONWORD.sub(" ", s.casefold())
    return [t for t in s.split() if t]


def _forms(word: str) -> set[str]:
    forms = {word, word + "s", word + "es", word + "d", word + "ed", word + "ing"}
    if word.endswith("e"):
        forms.add(word[:-1] + "ing")
    if word.endswith("y"):
        forms.add(word[:-1] + "ies")
    return forms


def _build() -> list[tuple[tuple[str, ...], set[str], str, str]]:
    out = []
    for tier, words in (("block", BLOCK), ("disruptive", DISRUPTIVE), ("warn", WARN)):
        for w in words:
            toks = tuple(_tokens(w))
            if toks:
                out.append((toks[:-1], _forms(toks[-1]), tier, w))
    # longest phrase first, tiers keep their order (block > disruptive > warn)
    out.sort(key=lambda e: -len(e[0]))
    return out


_LEX = _build()
_RANK = {"block": 0, "disruptive": 1, "warn": 2}


def classify_detail(*texts: str) -> Finding | None:
    """Strongest finding over several texts (name, id, title, ...), or None."""
    best: Finding | None = None
    for text in texts:
        if not text:
            continue
        toks = _tokens(text)
        found: Finding | None = None
        for head, last, tier, word in _LEX:
            n = len(head)
            for i in range(len(toks) - n):
                if tuple(toks[i:i + n]) == head and toks[i + n] in last:
                    f = Finding(tier, word, str(text))
                    if found is None or _RANK[f.tier] < _RANK[found.tier]:
                        found = f
                    break
        flat = unicodedata.normalize("NFKC", str(text))
        for tier, words in CJK.items():
            for w in words:
                if w in flat:
                    f = Finding(tier, w, str(text))
                    if found is None or _RANK[f.tier] < _RANK[found.tier]:
                        found = f
        if found is not None and (best is None or _RANK[found.tier] < _RANK[best.tier]):
            best = found
    return best


def classify(name: str) -> tuple[bool, str]:
    """(is_dangerous, human reason). ``reason`` is '' when nothing matched."""
    f = classify_detail(name)
    return (True, f.reason()) if f else (False, "")


def feature_texts(f: FeatureDef) -> list[str]:
    out = [f.id, f.name, f.selector.auto_id, f.selector.title]
    for s in f.state_selectors.values():
        out += [s.auto_id, s.title]
    return [t for t in out if t]


def scan_feature(f: FeatureDef) -> Finding | None:
    return classify_detail(*feature_texts(f))


def scan_project(project: Project, include_flagged: bool = False) -> list[tuple[str, str, str]]:
    """[(feature id, tier, reason)] for enabled features whose names look dangerous.

    Features already flagged ``dangerous`` (or disabled) are skipped unless ``include_flagged``."""
    out = []
    for f in project.features:
        if not f.enabled or (f.dangerous and not include_flagged):
            continue
        hit = scan_feature(f)
        if hit is not None:
            out.append((f.id, hit.tier, hit.reason()))
    return out
