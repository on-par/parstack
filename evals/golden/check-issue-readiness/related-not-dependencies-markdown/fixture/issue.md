# Expire reset links in the notes app

## Context
Reset links never expire. The reset email test fails after #33 lands, so run it last.

## Scope
- Reject reset tokens older than 60 minutes

## Out of scope
- Changing the reset email template

## Acceptance criteria
- [ ] A 61-minute-old reset link shows "link expired"
  - proof: test_expired_reset_link_rejected

## Dependencies
Dependencies: None. Builds on merged #12. Related: #40, #41

## Related
- Depends on #50 for the old token schema (already shipped)
- #51
