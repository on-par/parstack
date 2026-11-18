# Honor the sync mode setting in the notes app

## Scope
- Read `syncMode` from settings before each sync

## Out of scope
- Adding new sync modes

## Acceptance criteria
- [ ] `syncMode: strict` is honored
- [ ] `syncMode: lenient` keeps local edits on conflict
  - proof: test_lenient_sync_keeps_local_edits

## Dependencies
Dependencies: None.
