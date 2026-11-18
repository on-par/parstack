# Expire reset links in the notes app

## Scope
- Reject reset tokens older than 60 minutes

## Out of scope
- Changing the reset email template

## Acceptance criteria
- [ ] A 61-minute-old reset link shows "link expired"
  - proof: test_expired_reset_link_rejected
- [ ] A 59-minute-old reset link still works
  - proof: `npm test -- reset`

## Assumptions
- No dependencies: the token table already stores issued-at
- Token age is measured in UTC
