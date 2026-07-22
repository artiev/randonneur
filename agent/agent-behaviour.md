# Agent Behaviour File

> Copy this file into a new project (conventionally at `agent/agent-behaviour.md`)
> and the agent is ready to go. It is a **generic template**: the rules below
> are project-agnostic, with the `photogravy` rework (the project this was
> distilled from) shown inline as the worked example. Replace the
> `photogravy`-flavoured examples with the new project's equivalents; keep the
> rules. Pair it with `agent-history.md` (per-project running log).

## How to use this file

- **Read it first**, every session, before touching code.
- It defines *how* the agent works: the stack, the coding/comment/docstring/
  logging/git/commit conventions, the commit-by-commit workflow, and the
  bug-hunt protocol.
- Where a rule says "ask", ask the human. Where it says "default", pick the
  obvious option, mention it, and proceed.
- Keep `agent-history.md` updated as work happens (see its own header).

---

## 1. Technology

- **Language: Python.** Default to the project's pinned minor (e.g. 3.9+);
  write code that runs on that floor.
- **Use the project venv** for running anything: `./.venv/bin/python`,
  `./.venv/bin/<entry>`. Don't `pip install` into the system Python.
- **Build/packaging**: follow what's already declared (pyproject + hatchling
  in the example). Don't switch build backends without asking.
- **Run the tool through its entry point** to verify, not via ad-hoc scripts
  only: e.g. `./.venv/bin/python -m <pkg> --version`, `./.venv/bin/<entry>`.
- *Example (photogravy):* Python 3.9+, hatchling, entry `photogravy =
  photogravy.cli:cli`. Deps: click, Pillow, InquirerPy (on prompt_toolkit),
  rich, docutils. README is RST, validated with
  `python -m docutils --strict README.rst`.

---

## 2. Working mode

- **Propose before you change.** For anything beyond a trivial fix, surface
  the approach (and ask a question or two to narrow it) before writing code.
  A direct, unambiguous instruction from the human is itself approval — act
  on it.
- **Section work into clean, atomic commits and pause between them.** This
  lets the human review and lets the session reset tokens between commits.
  After each commit, stop and wait unless told to continue.
- **One concern per commit.** A fix + its tests belong together; a refactor
  + a feature do not.
- **Confirm hard-to-reverse or outward-facing actions** before doing them
  (deletes, overwrites, sends to an external service, force-pushes). Approval
  in one context doesn't extend to the next. Before deleting/overwriting,
  *look at the target* — if it contradicts how it was described, surface that
  instead of proceeding.
- **Never terminate a process you did not start.** `kill`, `pkill`, `kill -9`,
  `killall`, or any other signal/termination mechanism is off-limits for
  processes the human started (other terminal sessions, their editor, their
  browser, their Docker containers, anything outside *this* Claude session's
  own background tasks). The rule applies even in auto / unattended mode,
  and even when the process is "blocking" something — ask the human first,
  or wait for them to act. The only processes the agent is allowed to signal
  are the ones it itself started in this session (a background `Bash` task
  with a known task ID, or a process the agent explicitly spawned with a
  captured PID for cleanup). If unsure who started it, don't kill it — ask.
  This is a hard rule that cannot be broken without specific, case-by-case
  user agreement; bulk "just clean it up" permission does not extend here.
- **Never commit or push unless asked.** When a commit *is* requested and
  you are on the default branch, **ask the human whether a feature branch
  is required** for the changes before doing anything — don't auto-branch,
  and don't commit to the default branch without that confirmation. Apply
  the rule once per request, up front; a single "branch or commit on
  `<branch>`?" covers the whole batch. End commit messages with the trailer
  below.
- **Report faithfully.** Tests failed? Show the output. Skipped a step? Say
  so. Done and verified? State it plainly. Don't hedge done work, don't hide
  failures.
- **Keep `agent-history.md` current — it is part of the work, not a
  cleanup.** Every commit gets a dated entry in the running log the *same
  session* the commit lands: the decision/what, the why, and (for a bug fix)
  a bug-hunt record per §9. A commit that ships without a matching history
  entry is incomplete work — treat writing the entry as a step of the commit,
  not an optional afterthought. If you resume a session and find the log
  behind the commits (history shows work the log doesn't), backfill it before
  doing new work. The gotchas in the bug-hunt records are the most valuable
  output of the project; a silent fix that isn't recorded is a fix the next
  session will re-discover.
- **Give a recommendation, not a survey.** When weighing options, pick one
  and say why; don't list every path you won't take. Don't re-derive facts
  already established, or re-litigate a decision the human already made.

---

## 3. Coding guidelines

- **Match the surrounding code.** Read the file before editing; mirror its
  indentation, naming, idiom, and comment density. New code should read like
  the old code wrote it.
- **Indentation: 2 spaces** (the example repo's convention). No tabs.
- **Line length:** keep lines comfortable; wrap long calls/signatures
  sensibly. Don't dogmatically fill to a column.
- **Naming:** `snake_case` for functions/vars, `PascalCase` for classes,
  `UPPER_SNAKE` for module constants. Prefix private module-level names
  with `_` (e.g. `_DEFAULT_OUTPUT`, `_console`).
- **Imports:** stdlib → third-party → local, one per line, grouped. Use
  explicit module aliases at import sites (`from x import y as y_screen`
  when a name collides or a short alias aids readability).
- **Mutability caution with third-party libraries.** Don't put mutable
  objects (dicts, lists, class instances) into values a library may copy.
  Prefer immutable handles (ints, strings, tuples of those) and re-resolve
  the real object on your side. *(See bug-hunt #2 in agent-history.md —
  InquirerPy's `dataclasses.asdict()` silently deep-copied Choice values.)*
- **Atomic, idempotent file writes** for config/state (write-temp +
  replace). Don't leave partial state on disk on failure.
- **Don't add dependencies** without asking.

---

## 4. Comments & docstrings

- **Every module starts with a docstring** explaining what the module is for
  and how it fits its neighbours. Keep it to what isn't obvious from the code.
- **Public/non-trivial functions get a one-line docstring** (imperative
  mood: "Opens the editor. Returns True on Save, False on Cancel."). Add a
  short body only when args/return/ordering/side-effects aren't obvious.
- **Comments explain *why*, not *what*.** The code says what; the comment
  says the constraint, the gotcha, or the reason. If you fix a non-obvious
  bug, leave a comment at the fix site so it doesn't regress.
- **Section dividers** for internal groups in a module use a box-drawing
  rule:
  ```python
  # ─── Internals ──────────────────────────────────────────────────────────────
  ```
- **No commented-out code** in committed work.
- *Example (photogravy `config_editor.py`):* module docstring describes the
  `edit_config(ctx, panel_title, sections) -> bool` contract and the
  section/field dict shapes; the asdict fix carries an inline `# …` block
  explaining *why* Choice values are index tuples, not the section dicts.

---

## 5. Logging

- **One logging style, configured once at startup, re-callable.** A single
  handler on the parent logger; toggling verbosity re-installs it (don't
  stack handlers).
- **Format:** `<symbol> <feature> · <message>` — a coloured severity symbol,
  the short feature name (the logger suffix), and the message. The symbol
  only signals severity at WARNING+; INFO/DEBUG share the dim middle-dot.
  ```text
  · contact_sheet · Found 36 images — building layout.
  ⚠ sanitize     · Test-run mode — no changes written to disk.
  ✗ exifs        · Could not read `DSCF0001.RAF.exif` — file missing.
  ```
- **Levels:** INFO by default (per-item progress demoted to DEBUG);
  `verbose=True` raises the whole tree to DEBUG for the full trace.
- **Render via rich** (coloured `Text`), `highlight=False`, so log messages
  aren't auto-styled.
- **Log, don't print** — except true preflight failures (e.g. a required
  external binary missing) which go to stderr and exit non-zero.
- *Example (photogravy `log.py`):* `configure_logging(verbose)` installs
  `_RichHandler` on the `photogravy` parent logger; `_SYMBOLS` maps levelno →
  (symbol, colour); the feature name is `record.name` after the
  `photogravy.` prefix.

---

## 6. Git style

- **Branches:** `feature/<short-slug>` for feature work. When a commit is
  requested while on the default branch, **ask whether a feature branch is
  required** for the changes (see §2); commit directly to the default branch
  only if the human says so.
- **Commits are atomic and Conventional-Commits-flavoured:**
  - Type prefix, capitalized: `Feat:`, `Fix:`, `Refactor:`, `Doc:`,
    `Chore:`, and `Data:` (see the data-track rule below).
  - Subject in the **imperative mood** and **ends with a `.`**,
    always — one-liner or multi-line alike. (This is a deliberate
    departure from standard Conventional-Commits, which drops the
    trailing period on multi-line subjects; this project prefers the
    period as a consistent end-of-subject marker regardless of
    whether a body follows.)
  - Examples from this rework:
    `Feat: Flatten the TUI into a Home menu with full-width panels.`
    `Fix: Persist config editor field edits (InquirerPy deep-copies Choice values).`
    `Refactor: Demote exif to a core prerequisite and rework common config.`
- **Each new GPX track (or other dataset file) is its own `Data:`
  commit, separate from code.** A track import must not ride along on
  a `Feat:`/`Fix:` commit — it would clutter that commit's diff and
  mix a data addition with a code change. Stage just the data file
  (+ its `agent-history.md` entry) and commit as
  `Data: <one-line description>.` (the subject ends with a period
  per the rule above). Multiple new tracks in one go are still
  one `Data:` commit only if they belong together (e.g. a batch
  import); otherwise one track per `Data:` commit. *(randonneur
  project rule — the dataset lives in-repo under `data/`.)*
- **Body explains *why*, not *what*.** What happened is in the diff; the
  body carries the root cause, the constraint, and the reasoning. For a
  bug-fix commit, lead the body with the root cause.
- **Every commit message ends with:**
  ```
  Co-Authored-By: Claude <noreply@anthropic.com>
  ```
- Use `git commit -F -` with a heredoc for multi-line messages; never
  `--no-verify` unless the human asks.
- Don't push, rebase, or force-push unless asked.

---

## 7. Commit-by-commit workflow

1. **Understand** the slice (read the files; confirm against the plan /
   the human's ask).
2. **Implement** just that slice — nothing from the next commit.
3. **Verify** before committing: run the tool, the smoke/pty check, the
   linter/validator (e.g. `docutils --strict` for RST). Failing tests =
   not done; say so with the output.
4. **Clean up** scratch artifacts (temp files, global state you mutated
  during verification) so they don't leak into the commit or the
  environment.
5. **Stage + commit** with the message style above (type prefix, imperative
   subject, why-body, `Co-Authored-By` trailer).
6. **Record in `agent-history.md`** the same session: a dated entry for the
   commit (decision/what + why), and — if the commit fixed a bug — a
   bug-hunt record per §9. Shipping the commit without its history entry
   is incomplete work (see §2).
7. **Pause.** Stop and let the human review / let the session reset before
   the next commit. Don't chain commits unbidden.

---

## 8. Verification & testing approach

- **Test against the real thing, not only stubs.** Monkeypatch harnesses are
  fast but they *replace* library calls and can mask real library behaviour
  (a stub that returns the queued value bypasses copying, validation,
  rendering, signal handling that the real call does). Always also exercise
  the real library path.
- **For TUIs/terminals, drive a real pty.** `pty.fork()` + feed keystrokes +
  a reader thread; set the winsize via
  `fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))`
  (InquirerPy crashes with `ZeroDivisionError` at width 0). Pty keystroke
  timing is flaky — give generous delays, and prefer *asserting on returned
  values / spied kwargs* over *asserting on rendered cursor position* when
  navigation is involved.
- **Reproduce bugs in the real environment before fixing.** A bug that only
  shows under the real library won't be caught by a stub, and a "fix" that
  only passes under a stub isn't a fix.
- **Re-validate docs.** RST → `python -m docutils --strict`; re-run after
  edits.

---

## 9. Bug-hunt protocol

1. **Reproduce in the real environment** (real pty / real entry point), not
   a stub. If a stub "passes" but the human sees the bug, the stub is hiding
   it — trust the human's report over the stub.
2. **Form a hypothesis, then *try to falsify it*.** Don't confirm a fix
   against a test that shares the fix's blind spot.
3. **Instrument, don't guess.** Add spies that log the *identity* and value
   of the data crossing a boundary (`id(obj)`, before/after prints). Two
   different ids where you expected one = a copy is happening somewhere.
4. **Find the root cause, not the symptom.** If a fix at one layer "works"
   but the bug persists, you fixed the wrong layer — keep digging.
5. **Fix at the root**, and leave a comment at the fix site explaining the
   gotcha so it doesn't regress.
6. **Verify end-to-end in the real environment** — through the real entry
   point, with the real library — that the fix resolves the reported
   symptom and didn't regress adjacent behaviour.
7. **Record the hunt** in `agent-history.md` (bug, hypothesis, root cause,
   fix, learning). The gotchas are the most valuable output of the session.

---

## 10. Communication

- Lead with the conclusion; evidence after.
- Cite `file:line` (clickable) for code references.
- Don't preface, recap, or say "I'll continue" — just continue.
- When you need the human to run something interactive (e.g. a login),
  suggest the `! <command>` form.
- When a decision is genuinely the human's and changes what you'd build,
  ask (a small, focused question) — otherwise pick the sensible default and
  say so.

---

## Appendix A — photogravy quick reference (the worked example)

- **Stack:** Python 3.9+, hatchling, entry `photogravy = photogravy.cli:cli`.
  Deps: click, Pillow, InquirerPy 0.3.4, rich 15, docutils 0.23. Run via
  `.venv/bin/python` / `.venv/bin/photogravy`.
- **Layout:** `photogravy/` package — `cli.py` (click groups), `config.py`
  (global + per-folder session), `log.py` (rich logging), `features/`
  (each feature: `requires`, `CONFIG_CLASS`, `run(cfg)->Report`),
  `tui/` (`app.py` → Home screen → feature pages; `screens/config_editor.py`
  is the reusable inline editor), `lib/` (crawler, exifs, sanitizers).
- **Config:** global `~/.config/photogravy/config.json` (tool state) + per-
  directory sectioned `photogravy.json` (common/sanitize/contact_sheet).
  Atomic writes. Start-fresh on schema change.
- **Feature contract:** each feature module declares `requires: list`,
  a `CONFIG_CLASS` dataclass with `from_dict(directory, common, data)`, and
  `run(cfg) -> Report`. Orchestrator `execute_feature(directory, slug,
  common, feature_cfg)` refreshes EXIF sidecars (gated by
  `common.reload_exif`) then runs one feature.
- **Editor gotcha (fixed):** `edit_config` Choice values are
  `('field', section_index, field_index)` tuples, *not* the section/field
  dicts, because InquirerPy `asdict()`-copies Choice values. See
  `agent-history.md` bug #2.

---

*Keep this file stable as the contract; evolve it deliberately, and note the
evolution in `agent-history.md`.*