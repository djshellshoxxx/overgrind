"""Shared v2 contracts (pure data + Protocols, no logic). Frozen: change only via the lead.

Every module imports its types from here. JSON (de)serialisation lives in project.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, runtime_checkable

SCHEMA_VERSION = 2
Value = Any  # bool | int | float | str

# ---------------------------------------------------------------- model
@dataclass(frozen=True)
class Selector:
    """Multi-strategy element locator, ordered by stability: auto_id > path > title+type > index."""
    auto_id: str = ""
    title: str = ""
    control_type: str = ""
    class_name: str = ""
    path: tuple[str, ...] = ()      # ancestor titles/ids from the window down
    index: int | None = None        # nth match among identical siblings (last resort)

@dataclass(frozen=True)
class Provenance:
    source: str = "manual"          # uia | manual | inferred | v1-import
    confidence: str = "manual"      # verified | inferred | manual | unknown
    note: str = ""

@dataclass(frozen=True)
class Domain:
    """kind: boolean | integer | float | choice | text."""
    kind: str
    minimum: float | int | None = None
    maximum: float | int | None = None
    step: float | int | None = None
    options: tuple[str, ...] = ()
    max_length: int | None = None
    alphabet: str = ""              # "" -> default alphabet

@dataclass(frozen=True)
class FeatureDef:
    """kind: toggle (states off/on) | multistate (named states, e.g. Off/Low/High) | action (event, no state)."""
    id: str
    name: str = ""
    kind: str = "toggle"
    states: tuple[str, ...] = ("off", "on")
    default_state: str = "off"
    selector: Selector = Selector()
    state_selectors: dict[str, Selector] = field(default_factory=dict)  # per-state (radio/tab style)
    enabled: bool = True            # included in the test
    dangerous: bool = False         # never auto-activated unless user clears the flag
    provenance: Provenance = Provenance()

@dataclass(frozen=True)
class ControlDef:
    id: str
    kind: str                       # == domain.kind
    selector: Selector
    domain: Domain                  # what the app/UIA says (original)
    fuzz_domain: Domain | None = None   # tester-restricted subset; None -> same as domain
    default: Value = None
    scope: tuple[str, ...] = ()     # feature ids that must ALL be non-default-off ("active") for the control to be written; () = global
    skip: bool = False
    read_only: bool = False
    sensitive: bool = False         # redact values in evidence
    provenance: Provenance = Provenance()

@dataclass(frozen=True)
class ConstraintDef:
    """kind: mutex(features: at most one active) | requires(feature -> needs: all listed active)
    | forbid(assignment: partial {feature: state} that is invalid)."""
    kind: str
    features: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()
    assignment: dict[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class StopConditions:
    until_stopped: bool = True      # default: run until user stops
    max_seconds: float | None = None
    max_cases: int | None = None
    max_unique_errors: int | None = None
    first_error: bool = False
    coverage_complete: bool = False  # stop after first full feature-state epoch

@dataclass(frozen=True)
class RunConfig:
    strategy: str = "state_cover"   # state_cover | ordered_pairs | twise | boundary | adaptive | chaos
    strategy_options: dict[str, Any] = field(default_factory=dict)  # e.g. {"t": 2}
    seed: int = 1
    include_all_off: bool = True
    reset_policy: str = "every_case"    # every_case | interval | never
    reset_interval: int = 50
    settle_ms: int = 25
    op_timeout_ms: int = 5000       # per adapter operation
    case_timeout_ms: int = 30000
    hang_threshold_ms: int = 5000   # unresponsive this long => hang
    restart_on_crash: bool = False
    pause_counts_toward_time: bool = False
    ring_size: int = 200
    invalid_values: bool = False    # opt-in out-of-range probing
    confirm_runs: int = 2           # replays used to classify reliability
    capture_screenshot: bool = True
    capture_dump: bool = False

@dataclass(frozen=True)
class TargetRef:
    kind: str = "process"           # exe | process | window
    exe_path: str = ""
    args: tuple[str, ...] = ()
    process_name: str = ""
    pid: int | None = None
    title_regex: str = ""

@dataclass(frozen=True)
class Project:
    name: str
    target: TargetRef
    features: tuple[FeatureDef, ...]
    controls: tuple[ControlDef, ...] = ()
    constraints: tuple[ConstraintDef, ...] = ()
    run: RunConfig = RunConfig()
    schema_version: int = SCHEMA_VERSION
    uuid: str = ""

# ---------------------------------------------------------------- runtime
@dataclass(frozen=True)
class Case:
    seq: int
    state: dict[str, str]           # feature id -> state (every non-action feature)
    values: dict[str, Value]        # control id -> value (all fuzzable controls)
    order: tuple[str, ...] = ()     # feature ids / action ids in application order
    dwell_ms: int = 0
    tags: tuple[str, ...] = ()      # e.g. ("epoch:3", "boundary")

@dataclass(frozen=True)
class Action:
    """One ring-buffer entry. kind: state | control | invoke | reset | wait | restart."""
    kind: str
    target: str
    value: Value = None
    t_ms: float = 0.0
    ok: bool = True

@dataclass(frozen=True)
class Health:
    status: str = "ok"              # ok | dead | hung | modal | missing
    detail: str = ""

@dataclass(frozen=True)
class ElementInfo:
    """One inspected element (read-only snapshot)."""
    selector: Selector
    name: str
    control_type: str
    depth: int = 0
    parent: int | None = None       # index into the snapshot list
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    value: Value = None
    patterns: tuple[str, ...] = ()
    suggested_kind: str = ""        # boolean|integer|float|choice|text|action|"" 
    suggested_domain: Domain | None = None
    confidence: str = "unknown"     # verified | inferred | unknown

@dataclass(frozen=True)
class Outcome:
    ok: bool = True
    category: str = ""              # crash | hang | dialog | assertion | resource | harness
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

@dataclass
class ErrorEvent:
    id: str
    ts: str                         # ISO-8601 UTC
    run_id: str
    category: str
    signature: str                  # dedup fingerprint
    message: str
    case: Case
    history: list[Action]           # ring buffer at failure time
    env: dict[str, Any] = field(default_factory=dict)
    attachments: dict[str, str] = field(default_factory=dict)   # name -> relative path
    count: int = 1
    reliability: str = "unconfirmed"    # unconfirmed | reliable | flaky | not_reproducible
    reduced: dict[str, Any] | None = None

@dataclass(frozen=True)
class Coverage:
    states_total: int | None = None     # None = not enumerable
    states_visited: int = 0
    epochs: int = 0
    tway_t: int | None = None
    tway_total: int | None = None
    tway_covered: int = 0
    buckets_total: int = 0
    buckets_visited: int = 0
    guarantee: str = ""             # honest human-readable statement of what has been covered

@dataclass(frozen=True)
class RunStats:
    state: str = "idle"             # idle | running | paused | stopping | finished
    elapsed_s: float = 0.0
    cases: int = 0
    cases_per_s: float = 0.0
    errors_total: int = 0
    errors_unique: int = 0
    crashes: int = 0
    hangs: int = 0
    harness_errors: int = 0
    restarts: int = 0
    coverage: Coverage = Coverage()
    current: str = ""               # one-line description of the case in flight
    recent: tuple[Action, ...] = () # last few actions (ring tail)
    stop_reason: str = ""

# ---------------------------------------------------------------- errors / protocols
class HarnessError(Exception):
    """Automation failed (element missing, timeout in OUR driver). Never an app defect."""

class TargetFailure(Exception):
    """Evidence of an app defect (crash/hang/dialog). `.category` in crash|hang|dialog|assertion."""
    def __init__(self, category: str, message: str = ""):
        super().__init__(message)
        self.category = category

@runtime_checkable
class TargetAdapter(Protocol):
    def attach(self, target: TargetRef) -> None: ...        # launch/attach; idempotent
    def detach(self, kill: bool = False) -> None: ...
    def inspect(self) -> list[ElementInfo]: ...             # read-only
    def set_feature_state(self, feature: FeatureDef, state: str) -> None: ...
    def invoke(self, feature: FeatureDef) -> None: ...
    def set_control(self, control: ControlDef, value: Value) -> None: ...
    def get_control(self, control: ControlDef) -> Value: ...
    def reset(self, project: Project) -> None: ...          # restore defaults
    def wait_quiescent(self, timeout_ms: int) -> None: ...
    def health(self) -> Health: ...
    def screenshot(self, path: str) -> bool: ...
    def pid(self) -> int | None: ...

@runtime_checkable
class CaseGenerator(Protocol):
    """Bounded-memory, deterministic given (project, seed, checkpoint)."""
    def __iter__(self) -> Iterator[Case]: ...
    def coverage(self) -> Coverage: ...
    def feedback(self, case: Case, outcome: Outcome) -> None: ...
    def checkpoint(self) -> dict[str, Any]: ...             # small JSON-able resume state
    def restore(self, state: dict[str, Any]) -> None: ...

@dataclass(frozen=True)
class AdapterSpec:
    """Picklable adapter factory so the worker PROCESS can build it: 'pkg.module:callable' + kwargs."""
    factory: str
    kwargs: dict[str, Any] = field(default_factory=dict)
