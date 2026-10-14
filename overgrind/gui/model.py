"""ProjectModel: single owner of the Project, with undo/redo and a dirty flag."""
from __future__ import annotations

import dataclasses as dc
from typing import Callable

from PySide6.QtCore import QObject, Signal

from ..types import (
    ConstraintDef, ControlDef, FeatureDef, Project, RunConfig, StopConditions, TargetRef,
)

_MAX_UNDO = 200


def empty_project(name: str = "Untitled project") -> Project:
    return Project(name=name, target=TargetRef(), features=())


class ProjectModel(QObject):
    changed = Signal()            # any project edit / load
    dirtyChanged = Signal(bool)
    pathChanged = Signal(str)

    def __init__(self, project: Project | None = None, parent=None):
        super().__init__(parent)
        self._project = project or empty_project()
        self._path = ""
        self._dirty = False
        self._undo: list[tuple[str, Project]] = []
        self._redo: list[tuple[str, Project]] = []
        self.adapter_spec = None      # AdapterSpec used for runs / scanning (None -> real UIA)
        self.demo = False

    # ---- read
    @property
    def project(self) -> Project:
        return self._project

    @property
    def path(self) -> str:
        return self._path

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def stop(self) -> StopConditions:
        return _stop_from_options(self._project)

    def feature(self, fid: str) -> FeatureDef | None:
        return next((f for f in self._project.features if f.id == fid), None)

    def control(self, cid: str) -> ControlDef | None:
        return next((c for c in self._project.controls if c.id == cid), None)

    def unique_id(self, base: str, existing: set[str] | None = None) -> str:
        used = existing if existing is not None else {f.id for f in self._project.features} | {c.id for c in self._project.controls}
        base = "".join(ch if ch.isalnum() or ch in "_-." else "_" for ch in (base or "item")).strip("_") or "item"
        cand, n = base, 2
        while cand in used:
            cand = f"{base}_{n}"; n += 1
        return cand

    # ---- write (all edits go through here)
    def edit(self, label: str, fn: Callable[[Project], Project]) -> None:
        new = fn(self._project)
        if new == self._project:
            return
        self._undo.append((label, self._project))
        del self._undo[:-_MAX_UNDO]
        self._redo.clear()
        self._project = new
        self._set_dirty(True)
        self.changed.emit()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str:
        return self._undo[-1][0] if self._undo else ""

    def undo(self) -> None:
        if not self._undo:
            return
        label, prev = self._undo.pop()
        self._redo.append((label, self._project))
        self._project = prev
        self._set_dirty(True)
        self.changed.emit()

    def redo(self) -> None:
        if not self._redo:
            return
        label, nxt = self._redo.pop()
        self._undo.append((label, self._project))
        self._project = nxt
        self._set_dirty(True)
        self.changed.emit()

    def load_project(self, project: Project, path: str = "", dirty: bool = False) -> None:
        self._project = project
        self._undo.clear(); self._redo.clear()
        self._path = path
        self.pathChanged.emit(path)
        self._set_dirty(dirty)
        self.changed.emit()

    def mark_saved(self, path: str) -> None:
        self._path = path
        self.pathChanged.emit(path)
        self._set_dirty(False)

    def _set_dirty(self, v: bool) -> None:
        if v != self._dirty:
            self._dirty = v
            self.dirtyChanged.emit(v)

    # ---- typed helpers
    def rename(self, name: str) -> None:
        self.edit("Rename project", lambda p: dc.replace(p, name=name))

    def set_target(self, target: TargetRef) -> None:
        self.edit("Change target", lambda p: dc.replace(p, target=target))

    def add_feature(self, f: FeatureDef) -> None:
        self.edit("Add feature", lambda p: dc.replace(p, features=p.features + (f,)))

    def update_feature(self, fid: str, **kw) -> None:
        def fn(p: Project) -> Project:
            return dc.replace(p, features=tuple(dc.replace(f, **kw) if f.id == fid else f for f in p.features))
        self.edit("Edit feature", fn)

    def remove_feature(self, fid: str) -> None:
        def fn(p: Project) -> Project:
            cons = tuple(c for c in p.constraints if fid not in c.features and fid not in c.needs and fid not in c.assignment)
            ctrls = tuple(dc.replace(c, scope=tuple(s for s in c.scope if s != fid)) for c in p.controls)
            return dc.replace(p, features=tuple(f for f in p.features if f.id != fid), constraints=cons, controls=ctrls)
        self.edit("Remove feature", fn)

    def add_control(self, c: ControlDef) -> None:
        self.edit("Add control", lambda p: dc.replace(p, controls=p.controls + (c,)))

    def update_control(self, cid: str, **kw) -> None:
        def fn(p: Project) -> Project:
            return dc.replace(p, controls=tuple(dc.replace(c, **kw) if c.id == cid else c for c in p.controls))
        self.edit("Edit control", fn)

    def remove_control(self, cid: str) -> None:
        self.edit("Remove control", lambda p: dc.replace(p, controls=tuple(c for c in p.controls if c.id != cid)))

    def add_constraint(self, c: ConstraintDef) -> None:
        self.edit("Add rule", lambda p: dc.replace(p, constraints=p.constraints + (c,)))

    def remove_constraint(self, index: int) -> None:
        def fn(p: Project) -> Project:
            return dc.replace(p, constraints=tuple(c for i, c in enumerate(p.constraints) if i != index))
        self.edit("Remove rule", fn)

    def set_run(self, **kw) -> None:
        self.edit("Change plan", lambda p: dc.replace(p, run=dc.replace(p.run, **kw)))

    def reset_run(self) -> None:
        self.edit("Reset plan to defaults", lambda p: dc.replace(p, run=RunConfig()))

    def set_strategy_option(self, key: str, value) -> None:
        def fn(p: Project) -> Project:
            opts = dict(p.run.strategy_options)
            if value is None:
                opts.pop(key, None)
            else:
                opts[key] = value
            return dc.replace(p, run=dc.replace(p.run, strategy_options=opts))
        self.edit("Change plan", fn)

    def set_stop(self, stop: StopConditions) -> None:
        """Stop conditions live in RunConfig.strategy_options['stop'] so they are saved with the project."""
        self.set_strategy_option("stop", dc.asdict(stop))


def _stop_from_options(p: Project) -> StopConditions:
    raw = p.run.strategy_options.get("stop")
    if isinstance(raw, dict):
        try:
            return StopConditions(**{k: v for k, v in raw.items() if k in {f.name for f in dc.fields(StopConditions)}})
        except TypeError:
            pass
    return StopConditions()
