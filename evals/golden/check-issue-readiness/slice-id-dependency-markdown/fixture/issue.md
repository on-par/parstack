# Show "link expired" on the notes app reset page

## Value
A user with an old reset link sees why it failed.

## Scope
- Render the expired state on the reset page

## Acceptance criteria
- [ ] An expired reset link shows "link expired" and a resend button
  - proof: test_reset_page_shows_expired_state

## Out of scope
- Changing the reset email template

## Dependencies
- Depends on S1
- Depends on #40
