# Overgrind

**Product direction:** [Continuous Standalone GUI Fuzzing v0.2 specification](docs/specs/continuous-gui-fuzzing-v0.2.md). The goal is an independent Windows GUI application allowing users to select a target, define each feature's default state, limit control ranges, run indefinitely by default, and persist only errors. **This is planned, not yet available.** The current v0.1 CLI remains a finite ordered-pair prototype.

Overgrind is an extensible, deterministic **GUI feature-combination fuzzing and stress-test runner**. It inventories exposed controls, models each control's permitted input domain, and tests every *ordered* pair of main application features with randomized values.

**Current status: initial CLI and Windows UI Automation adapter (experimental).** The deterministic engine and unit tests run on other operating systems; the interactive Windows adapter requires Windows and an already running target app. It is not a general-purpose binary parser or memory-coverage fuzzer.

## Model

- **F**: developer-confirmed main features.
- **C**: discovered or developer-specified GUI controls.
- **R(c)**: valid domain for each control (integer interval, float interval, bool, enumeration, bounded text).
- **X = F²**: ordered feature pairs, including self-pairs, e.g. A→A, A→B, B→A, B→B.
- **S**: random trials per ordered feature pair.
- **T = F² × S**: cases per campaign.

Every test case samples *all* C controls, then activates the two features in pair order and applies the eligible control values. A control with optional requires_features is only written when those features are active in the pair. This supports GUI widgets that are disabled outside a particular effect section. The case order is shuffled using a deterministic PRNG. Numeric values emphasize min/max/zero/midpoint in addition to uniform samples. The same manifest, Python version and seed reproduce the generated test sequence. Timing, UI state, and outside processes may still introduce nondeterminism.

Continuous control domains and arbitrary text have too many possibilities to enumerate exhaustively. **Complete feature-pair coverage does not imply complete GUI state or interaction-path coverage.** The runner caps campaigns at 250,000 cases per run to avoid allocating unbounded scheduling memory.

## Install and test

Python 3.10+ is required. From a local checkout:

    python -m pip install -e .
    python -m unittest discover -s tests -v
    python -m overgrind plan --config examples/demo.json --samples 2 --seed 123 --limit 10

For native Windows UI Automation, also install:

    python -m pip install -e ".[windows]"

The pywinauto backend runs in a **logged-in interactive Windows desktop session**, at an appropriate privilege level for the application under test.

## Inspect a GUI

Start the application, then run:

    python -m overgrind inspect --title-regex "Your App.*" --output controls.json

Inspection is **read-only**. The JSON catalog lists observed labels, automation IDs, UIA types and suggested value domains or feature actions. This is a discovery aid, **not an executable manifest**.

Review the inventory and author a validated manifest like [examples/demo.json](examples/demo.json). Replace all sample IDs with real identifiers from your application. Confirm controls on secondary pages and dialogs: hidden, custom-drawn, virtualized or dynamically created controls may not appear in the initial inventory. Where the OS cannot supply numerical limits, enumeration options, or a text field's maximum length, the developer must enter them.

Developer-provided manifests are also supported when UIA is incomplete. For custom-drawn JUCE knobs or sliders, a dedicated adapter or test-only automation bridge may be needed because not all values are accessible through Windows UI Automation.

### Manifest schema

    {
      "title_regex": "MyApp.*",
      "features": [
        {"id": "delay", "mode": "enable",
         "selector": {"auto_id": "delayToggle", "control_type": "CheckBox"}},
        {"id": "reverb", "mode": "enable",
         "selector": {"auto_id": "reverbToggle", "control_type": "CheckBox"}}
      ],
      "controls": [
        {"id": "wet", "kind": "float", "minimum": 0, "maximum": 1,
         "requires_features": ["delay"],
         "selector": {"auto_id": "wet", "control_type": "Slider"}},
        {"id": "quality", "kind": "choice", "options": ["Low", "High"],
         "selector": {"auto_id": "quality", "control_type": "ComboBox"}}
      ]
    }

Control kinds: **integer**, **float**, **boolean**, **choice**, **text** (requires max_length, optional alphabet). A control's optional requires_features is a list of feature ids that must all be active in the current pair before that widget is set. All controls still receive sampled values in the record; applied_controls identifies which were written. Selectors use a stable automation ID, or a visible title with an optional control_type. Unique, stable selectors matter.

Feature modes:
- **enable**: an idempotent UIA toggle action (use for independent checkboxes). Before each case, the Windows adapter attempts to disable all such features, then enables the selected pair. Self-pair A→A enables A twice without toggling it back off.
- **invoke** (default): invokes/clicks each control in sequence; button/tab/menu commands often fall in this category. These are **sequential interactions**, not proof two features are simultaneously active. The generic adapter cannot undo arbitrary invoked actions. Implement a purpose-built adapter reset if isolated interaction testing is required.

Do not automatically treat destructive commands, Save/Open operations, or mutually exclusive tabs as independent enable-able main features. A and B must be simultaneously addressable for an on/off pair to represent a combined feature state.

## Preview and run

    python -m overgrind plan --config examples/demo.json --samples 100 --seed 2048 --limit 12

Review cases and selectors first. Execute against an application you control, preferably in a disposable test profile:

    python -m overgrind run --config examples/demo.json --samples 100 --seed 2048 --settle-ms 25 --report reports/run-2048.jsonl --execute

A run deliberately requires **--execute**. Each case is written as one JSON Lines record with case index, seed, ordered pair, exact sampled inputs, the controls actually applied, elapsed time, status, and error. Output files are create-only: existing evidence is never overwritten. Use --stop-on-failure to end on the first error. Exit code 0 = all cases passed; 1 = some failed; 2 = config/runtime problem.

To replay just case index 127 from a 100-samples-per-pair campaign:

    python -m overgrind run --config examples/demo.json --samples 100 --seed 2048 --case-index 127 --report reports/replay-127.jsonl --execute

Keep the same manifest, seed, samples, Python version, and application build. The runner executes only the selected case; its original index and exact sampled inputs are preserved. UI state, timing, and external effects may still differ.

## Architecture

    overgrind/
      models.py       # Validated developer-facing manifest
      sampling.py     # Type-aware deterministic input generation
      planning.py     # All ordered pairs × S, shuffled seed-based schedule
      runner.py       # Adapter protocol, case execution, JSONL evidence
      windows_uia.py  # Optional native Windows implementation + inventory
      __main__.py     # inspect, plan, run CLI
    tests/
      test_engine.py  # Deterministic planner, validation, runner tests

New application adapters implement set_control(control, value), activate_feature(feature), and healthy(), plus optional prepare_case(features) for state reset. They can reuse all other modules unchanged. An adapter may use application test APIs instead of simulating mouse input.

## Limits and next iterations

The first version records UIA automation failures and a basic window-liveness check. It **does not yet** measure memory leaks, CPU, audio glitches, visual corruption, deadlocks with a hard execution deadline, app restarts, screenshot evidence, full GUI discovery across tabs/dialogs, crash-dump signatures, coverage-guided mutation, shrinking, or native Windows integration tests. Some Windows GUI operations can block; per-case deadlines and process watchdogs are important follow-up work.

Recommended next modules: failing-case minimization, resource/CPU/memory sampler, watchdog/restart, screenshots and crash dumps, UIA control inventory export-to-manifest with approval workflow, secondary-page discovery, dependency-aware constraints (e.g. one knob controls another), stateful sequence fuzzing, web/Qt/JUCE adapters, and a GUI reporting dashboard.

## Development

    python -m unittest discover -s tests -v

The tests intentionally use an in-memory fake GUI backend, so deterministic coverage and reporting are testable without Windows or a real application. CI checks Python 3.10 and 3.13 on Linux. Windows automation needs its own dedicated GUI integration test environment before production claims can be made.

MIT licensed.
