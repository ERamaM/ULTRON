# 28 · Retire the sweep pipeline

Status: proposed. Delivery 3. Blocked until the scout mode passes deliveries 1 and 2 on two profiles.

## Current behavior

After the earlier deliveries, two search paths exist: the default sweep and the scout mode. The skill documents both, and the sweep keeps its own code: the query plan, seed rounds and packs, feed company hints, the heuristic evaluator as a filter, and the unattended sweep.

## Expected behavior

Once the scout mode has passed the [pass criteria](README.md#pass-criteria) on the first profile and on a second profile from another field, make it the only search path:

- `ultron run new` creates a scout run. `--mode` is removed.
- Remove the code only the sweep used: the mandatory query plan, `seeds plan` and seed rounds, sector packs as a discovery driver, feed company hints and the unattended sweep. `ultron watch` replaces `run --unattended`.
- Keep what the scout mode uses: the ATS clients, seed verification as the board resolver, job identity, the ledger, the pre-filter vocabulary, exports, and the CV, cover-letter and salary commands.
- Keep the heuristic evaluator only as the source of hints for the evaluation queue.
- Rewrite `SKILL.md` and its references around the six steps of the [new split](README.md#the-new-split). The skill stays in plain English, names no platform-specific tool and matches `ultron --help`.
- Update [architecture.md](../../architecture.md), the README and the wiki. Bump the version as a breaking change.

Old data must still open: run folders, the ledger and saved searches created by earlier versions. A saved search with an unattended sweep becomes a watch, and the user is told once.

## Acceptance cases

- No module, test or document refers to a removed command. `ultron --help` lists no removed command.
- A run folder and a ledger created by 0.4.1 open with `ultron runs list`, `ultron jobs` and the exports.
- A saved search created by 0.4.1 runs as a watch and reports the change.
- The labelled benchmark and the parity suite are updated for the hint-only role of the heuristics, with the reason for every changed case in the PR.
- `tests/test_architecture.py` passes, and no package is left without a documented role.
- The installer smoke tests pass on Windows, macOS and Linux.

## Verification

Run the full suite and the installer smoke tests. Repeat the live comparison with the retired code removed and confirm the scout numbers hold. Apply the [shared verification](README.md#verification).
