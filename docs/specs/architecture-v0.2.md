# Overgrind v0.2 architecture & module ownership
Contracts: `overgrind/types.py` (frozen). Product spec: `continuous-gui-fuzzing-v0.2.md`. Strategy: `../strategy/...`.
v0.1 modules (models/planning/sampling/runner/windows_uia/__main__) stay untouched and passing (P0-11).

| Module (owner) | Responsibility |
|---|---|
| `project.py`, `domain.py` (CORE) | Project<->JSON (v2), `migrate_v1(dict)->Project`, `validate(project)->list[str]` errors, `project_hash`, Domain sampling/bucketing/clamp/`effective_domain`, provenance |
| `constraints.py`, `states.py`, `scheduler.py`, `strategies/*` (ENGINE) | nogood compiler; valid-state enumeration/ranking (lazy, bounded); `make_generator(project)->CaseGenerator`; strategies state_cover, ordered_pairs, twise (streaming IPOG-style), boundary, adaptive, chaos; Coverage + honest guarantee text; checkpoint/restore |
| `evidence.py`, `reduce.py`, `diffrun.py`, `infer.py` (DIAG) | RingBuffer, failure classifier + fingerprint, atomic error-only `EvidenceStore` (list/load/dedup/redact), `Reducer` (ddmin + value shrink + reliability), `differential(project, specA, specB)`, dependency inference from observations |
| `controller.py`, `worker.py`, `watchdog.py`, `adapters/*`, `fixtures/*` (RUNTIME) | `RunController` (start/pause/resume/stop/stats/on_error), spawn-worker process + heartbeat watchdog, stop conditions, `adapters/mock.py` (MockAdapter, deterministic crash/hang injection, sample inspect tree), `adapters/uia.py` (pywinauto v2 adapter, Windows only, import-lazy), `adapters/procwatch.py` (psutil-free Win32 liveness/hang via ctypes), `fixtures/synthetic_app.py` (PySide6 target with toggles/sliders/combos, crash & hang buttons), CLI `__main__` v2 subcommands in `cli2.py` |
| `gui/*` (GUI) | PySide6 app `python -m overgrind.gui`; depends ONLY on types.py + the public APIs above |
| `docs/specs/features/*` (SPEC) | feature specs |

Public APIs other owners may call (names fixed):
- `project.load(path)->Project`, `project.save(p, path)`, `project.validate(p)->list[str]`, `project.migrate_v1(d)->Project`, `project.project_hash(p)->str`
- `domain.effective(control)->Domain`, `domain.sample(domain, rng)->Value`, `domain.boundary_values(domain)->list`, `domain.buckets(domain, n)->list[tuple]`
- `scheduler.make_generator(project, checkpoint=None)->CaseGenerator`, `scheduler.preview(project, n)->list[Case]`, `scheduler.estimate(project)->dict` (`valid_states`, `cartesian`, `text`)
- `evidence.EvidenceStore(dir)`: `.write(ErrorEvent)->str path`, `.list()->list[ErrorEvent]`, `.load(id)`, `.bump(signature)`; `evidence.RingBuffer(n)`: `.push(Action)`, `.snapshot()->list[Action]`; `evidence.classify(exc|Health, ...)->Outcome`; `evidence.fingerprint(...)->str`
- `reduce.Reducer(project, adapter_spec).reduce(event)->ErrorEvent` ; `reduce.classify_reliability(...)`
- `controller.RunController(project, adapter_spec: AdapterSpec, evidence_dir)`: `.start() .pause() .resume() .stop() .join(timeout) .stats()->RunStats .on_error(cb) .state`
- `adapters.mock.MockAdapter(crash_rate=0.0, hang_rate=0.0, seed=0, ...)`; AdapterSpec("overgrind.adapters.mock:MockAdapter", {...})
- `adapters.uia.UiaAdapter()` (Windows only)

Rules for every owner: only edit files you own (+ your `tests/test_<area>*.py`); stdlib `unittest`; Python>=3.10, no new runtime deps except GUI (`PySide6-Essentials`); no git commits (lead commits); deterministic, bounded memory; do not write success records to disk; no hard-coded absolute paths; cross-platform imports must not fail on Linux (Windows-only imports lazy). Run `python -m unittest discover -s tests` before reporting. Report in <=15 lines: files, public API actually implemented (deviations from above!), test count, known gaps.
