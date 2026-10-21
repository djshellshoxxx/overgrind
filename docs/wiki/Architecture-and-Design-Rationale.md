# Architecture and Design Rationale

Sources: README "Model" and "Architecture", `docs/strategy/`, `docs/specs/`. *(inferred)* marks conclusions from the code.

- **Ordered pairs, not combinations.** The model tests A→A, A→B, B→A and B→B, because order and self-pairs
  can expose different state transitions. Each campaign has T = F² × S cases.
- **Every case samples all controls.** Each test samples every control in the manifest, then activates the pair. This keeps the state space uniform across cases. *(from the README model section)*
- **Deterministic planning.** Seeded shuffling of the full schedule makes runs reproducible. *(planning.py)*
- **Adapter boundary.** `runner.py` depends on an adapter protocol. Windows UIA is one optional implementation, so other frameworks or application test APIs can plug in.
- **Honest feature modes.** `enable` (idempotent toggle) and `invoke` (sequential) are kept separate, because a sequential click does not prove two features are active at once.
- **Inspection is not manifest generation.** The inventory is a discovery aid that a developer reviews. This avoids executing unreviewed UI actions.
- **Scope.** Overgrind is not a binary parser or memory-coverage fuzzer; resource sampling, watchdogs and crash dumps are listed as future modules.
