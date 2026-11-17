# Expire password reset links after one hour

## Context
Reset links never expire today.

## Scope
- Store issued-at on the reset token
- Reject tokens older than 60 minutes

## Out of scope
- Changing the email template

## Acceptance criteria
- [ ] A 61-minute-old token is rejected with "link expired"
  - proof: test_expired_token_rejected
- [ ] A 59-minute-old token still works for a reset
  - proof: `npm test -- reset`

## Dependencies
- Depends on #41

```sh
# code blocks are ignored: TODO depends on #99
```
