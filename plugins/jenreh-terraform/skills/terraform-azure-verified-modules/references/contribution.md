# Contribution Standards

## GitHub Repository Branch Protection

**Severity:** MUST | **Requirement:** TFNFR3

Module owners **MUST** set branch protection policies on the default branch (typically `main`):

1. Require Pull Request before merging
2. Require approval of most recent reviewable push
3. Dismiss stale PR approvals when new commits are pushed
4. Require linear history
5. Prevent force pushes
6. Not allow deletions
7. Require CODEOWNERS review
8. No bypassing settings allowed
9. Enforce for administrators
