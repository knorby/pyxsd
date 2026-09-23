## Summary

<!-- What problem does this solve, and how? Link the issue for behavior
     changes and new features. -->

## Changes

-

## Verification

<!-- Commands you ran and their actual output. Do not check boxes for
     commands you did not run. -->

- [ ] `uv run pytest` passes
- [ ] `uvx ruff@0.16.6 check .` passes
- [ ] `uvx ruff@0.16.6 format --check .` passes
- [ ] `uv run mypy src/pyxsd` passes
- [ ] `uv run pre-commit run --all-files` passes
- [ ] Bug fixes include a test that failed before and passes now
- [ ] `CHANGELOG.md` and docs updated (or N/A: no public behavior change)

## Hygiene

- [ ] The diff is minimal and focused on one change (no drive-by
      refactors or reformatting)
- [ ] Everything stated here reflects something I actually ran or read
- [ ] If AI-assisted, I can explain every line of this change in review
