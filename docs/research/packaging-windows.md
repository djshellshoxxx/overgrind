# Research: packaging Overgrind (PySide6 + multiprocessing worker + UIA) for Windows

Tags: **[V]** confirmed from primary page content via search tool; **[S]** secondary/blog/vendor; **[K]** engineering knowledge, not re-verified; **[?]** open question to test empirically. `pyinstaller.org`, `learn.microsoft.com` and `docs.python.org` were not directly fetchable from the research sandbox (proxy 403/DNS), so those are [K] or via search summaries.

## 1. PyInstaller vs Nuitka

| Aspect | PyInstaller | Nuitka | Source |
|---|---|---|---|
| Model | bundles interpreter + bytecode; analysis by import scan + hooks | compiles Python to C, links libpython (needs C compiler, MSVC/MinGW on Windows) | [V] Qt docs say pyside6-deploy wraps Nuitka and compiles to C; [K] rest |
| Default Qt tooling | no official Qt wrapper | `pyside6-deploy` = official Qt wrapper around Nuitka; default **onefile**; `--mode=standalone` for dir; pinned Nuitka default e.g. 2.7.11 | [V] doc.qt.io deploy page |
| Build time | ~tens of seconds | minutes (one guide: 5-15 min for medium app vs ~30 s) | [S] pydevtools |
| Start-up | onedir ~36 ms vs onefile ~150 ms on a trivial Linux script; Nuitka onefile ~50 ms | single-author, single-machine, Linux, 20-line script -- **directional only** | [S] pistack.xyz (2026-09) |
| Size | disagreement between guides (Nuitka bigger vs smaller) | | [S] conflicting -> measure |
| Unused Qt modules | pulls binaries of every plugin it finds; delete unused plugins/modules (WebEngine!) before build | `pyside6-deploy` drops unreferenced QML-linked modules | [S] Qt forum (PyQt5-era); [V] Qt deploy docs |
| Hidden/dynamic imports | **misses string-based imports** (`importlib.import_module("pkg.mod")`) -> `--hidden-import`/`collect_submodules` | same class of issue, `--include-package` | [K] |
| AV false positives | reported on VirusTotal for fresh builds (vendor claims 5-15/70 -- unverified marketing); no solid head-to-head data | Nuitka onefile can also trigger detections | [S] mixed; **no rigorous data found** |

**Decision for alpha**: PyInstaller **onedir** (fastest start, no temp extraction, easiest to sign/inspect, simplest `multiprocessing` story); distribute as zip (portable) + per-user installer. Keep Nuitka standalone as a CI "experimental" job after alpha to compare size/start/AV on the real app. Do not use UPX [K: raises AV heuristics].

### Onefile vs onedir
Onefile extracts payload to a temp dir each launch (and runs from there) [S]: slower start, writes executable files into `%TEMP%` (AV heuristic trigger, [S] disputed -- one source claims onefile reduces file count/false positives; evidence points both ways), and **breaks the worker model** unless extraction is reused (each `spawn` worker re-executes the bundle bootloader). For a tool that spawns a UIA worker repeatedly and kills it on timeout, onedir is the robust choice.

## 2. multiprocessing in a frozen app (critical for `worker.py`)
- Windows uses `spawn`: a fresh interpreter re-imports the main module; unguarded top-level code recurses [S]; in a frozen exe the child is the same exe with `--multiprocessing-fork`-style args [K].
- Required: first statement under `if __name__ == "__main__":` is `multiprocessing.freeze_support()` [S + K official docs]. Official docs say it is a no-op off-Windows; a bug-tracker thread disputes for some frozen setups [S].
- **Do not import PySide6 at module top of anything the worker imports.** Worker start cost (Qt import ~100s of ms) and memory multiply by every respawn after a UIA stall. Enforce: `overgrind/worker.py`, `adapters/*`, `types.py` import-clean of Qt; add a unit test that `import overgrind.worker` leaves `"PySide6" not in sys.modules`.
- `AdapterSpec.factory = "pkg.module:callable"` is resolved with `importlib` at runtime => invisible to PyInstaller's analysis. Mitigation: PyInstaller spec `hiddenimports = collect_submodules("overgrind")` (+ `comtypes`, `pywinauto`) [K]. CI smoke test must start a worker from the frozen exe, not from source.
- `comtypes` (pywinauto UIA backend) generates `comtypes.gen.*` wrappers at runtime into a cache dir; frozen apps need pre-generated wrappers or a writable gen dir [K, well-known PyInstaller issue] **[?]** verify with the frozen build; fall back to a thin `ctypes`/own-IDL UIA binding if flaky.
- Set `multiprocessing.set_start_method("spawn")` explicitly (portable behaviour for tests on Linux too) [S].

## 3. CI: GitHub Actions `windows-latest` and UIA tests
- `windows-latest` migrated from Windows Server 2022 to **Windows Server 2025** (announced 2025-07-31, rollout Sep 2025); image tool set differs [V: github.blog changelog]. A later 2026 release note says the 2025 label moves to a Visual Studio 2026 image (June 2026) [S: release notes], dates inconsistent across sources -- pin `runs-on: windows-2025` (not `-latest`) for reproducibility [K].
- **Interactive desktop: not confirmed from any source retrieved.** Evidence is only analogical: CI services run in session 0 with no screen; GitLab's Windows runner documents session-0 limits incl. fixed 1024x768 [S]; a self-hosted-runner write-up needed auto-logon + RDP to give pywinauto a screen [S]. Treat as **[?]**: add a first CI step `desktop-probe` that prints `query session`, session id of the process (`ProcessIdToSessionId`), `System.Windows.Forms.Screen.AllScreens`, and runs a UIA smoke test on the synthetic target. Design tests in two tiers: (a) UIA-pattern tests (no mouse/keyboard injection; work without foreground focus in many cases [K]) -- required in CI; (b) SendInput/foreground tests -- run on self-hosted interactive VM, allowed-to-fail on hosted runners.
- Never promise unattended GUI runs on locked screens (already in spec); UAC secure desktop cannot be automated [K].
- Keep Linux CI for unit tests (MockAdapter) -- Windows-only imports lazy.
- Artifacts: upload zip + SHA-256 + signtool log; run the frozen-exe smoke test (`overgrind.exe --selftest` spawns worker, runs MockAdapter 100 cases) before signing.

## 4. Code signing and SmartScreen
- **Azure Artifact Signing** (formerly Trusted Signing): Basic $9.99/mo (5,000 signatures), Premium $99.99/mo (100k), overage $0.005/signature; supports public trust, private trust, test trust; "base reputation" with SmartScreen [V: azure.microsoft.com product page]. Eligibility (orgs US/CA/EU/UK, individuals US/CA only) comes from one blog -> **[?] verify on Microsoft's FAQ**.
- EV certificates **no longer grant instant SmartScreen reputation** (EV OIDs removed from Microsoft Trusted Root Program around Mar-Aug 2024; sources disagree on month); reputation accrues per file hash/publisher over downloads [S: todesktop.com, ssl.com, MS Q&A thread]. EV still shows verified org and is required for kernel drivers [S].
- CA/B Forum Code Signing BR v3.10.0: max validity for publicly trusted code-signing certs **460 days** for certs issued from 2026-03-01 [S: globalsign, digicert, others; one outlier says 200 days -- treat as error]. => plan annual renewal automation; private-key must live in HSM/cloud (no exportable PFX in CI) [K].
- Practice: sign every PE we ship (exe, dlls we build, installer) with RFC-3161 timestamp [K]; sign *before* zipping/installer; sign installer last. Unsigned alpha: document SmartScreen "More info -> Run anyway" and publish SHA-256.
- Self-signed/test certs are fine for CI smoke but do nothing for reputation.

## 5. Installers
| Option | Pros | Cons for Overgrind |
|---|---|---|
| **Portable zip (onedir)** | zero install, no admin, easiest AV triage | no Start-menu/uninstall; user unzips into protected dir |
| **Inno Setup** | free, mature (since 1997), per-user no-admin mode via `PrivilegesRequired=lowest` [K], scripts in Pascal, signing hook | proprietary format |
| NSIS | tiny, flexible, scripting | less structured, more footguns [K] |
| WiX (MSI) | enterprise deploy (GPO/SCCM) | XML-heavy; per-user MSI awkward [K] |
| MSIX | clean uninstall, auto-update, sandboxing | requires signing; file/registry virtualization breaks tools that read other processes/HKLM; machine-wide install needs elevation/PowerShell [S: Tech Community thread] -- **poor fit** for a tool that automates other apps |
**Choice**: zip + Inno Setup (per-user default, optional all-users). `uiAccess="true"` manifest (lets a non-elevated tool drive higher-integrity windows of same desktop) requires a signed binary installed in a secure location (`%ProgramFiles%`) [K] -> candidate Phase 2 option, not default.

## 6. AV / reputation hygiene
Onedir; no UPX; no self-modifying/packed stubs; reproducible CI build from tagged source; publish hashes; submit new releases to Microsoft Defender false-positive portal and major vendors [K/S]; avoid bundling Sysinternals ProcDump (EULA/redistribution) -- detect-on-PATH instead; document that the product intentionally sends input to other processes, reads their memory (dumps) and can terminate processes, which looks like malware to heuristics -- sign + clear manifest metadata (company, product, version).

## References
- https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html
- https://pyinstaller.org/en/stable/operating-mode.html ; https://docs.python.org/3/library/multiprocessing.html#multiprocessing.freeze_support (not re-fetched)
- https://www.pistack.xyz/posts/2026-09-10-python-executable-packaging-pyinstaller-nuitka-cxfreeze-comparison/ ; https://pydevtools.com/handbook/reference/nuitka/
- https://discuss.python.org/t/pyinstaller-false-positive/43171 ; https://forum.qt.io/post/592187
- https://github.blog/changelog/2025-07-31-github-actions-new-apis-and-windows-latest-migration-notice/
- https://azure.microsoft.com/en-us/products/artifact-signing
- https://www.globalsign.com/en/blog/code-signing-validity-changes ; https://www.todesktop.com/blog/posts/windows-apps-psa-ev-certs-do-not-grant-immediate-reputation-anymore
- https://sep.com/blog/executing-windows-desktop-app-system-tests-in-ci/ ; https://gitlab.com/gitlab-org/gitlab-runner/-/merge_requests/4994
- https://techcommunity.microsoft.com/idea/msixideas/machine-wide-provisioning-install-for-all-users/1781716
