# Testing & Documentation Requirements

## Test Tooling

**Severity:** MUST | **Requirement:** TFNFR5

**Required testing tools for AVM:**

- Terraform (`terraform validate/fmt/test`)
- terrafmt
- Checkov
- tflint (with azurerm ruleset)
- Go (optional for custom tests)

## Test Provider Configuration

**Severity:** SHOULD | **Requirement:** TFNFR36

For robust testing, `prevent_deletion_if_contains_resources` **SHOULD** be explicitly set to `false` in test provider configurations.

## Module Documentation Generation

**Severity:** MUST | **Requirement:** TFNFR2

- Documentation **MUST** be automatically generated via [Terraform Docs](https://github.com/terraform-docs/terraform-docs)
- A `.terraform-docs.yml` file **MUST** be present in the module root
