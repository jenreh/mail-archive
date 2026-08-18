---
name: terraform-azure-verified-modules
description: Azure Verified Modules (AVM) requirements and best practices for developing certified Azure Terraform modules. Use when creating or reviewing Azure modules that need AVM certification.
---

# Azure Verified Modules (AVM) Requirements

This guide covers the mandatory requirements for Azure Verified Modules certification. These requirements ensure consistency, quality, and maintainability across Azure Terraform modules.

**References:**

- [Azure Verified Modules](https://azure.github.io/Azure-Verified-Modules/)
- [AVM Terraform Requirements](https://azure.github.io/Azure-Verified-Modules/specs/terraform/)

## Reference Files

Read the relevant file below when working on that aspect of a module. This file (`SKILL.md`) covers the always-applicable MUST rules and the compliance checklist; the details live in `references/`.

- `references/code-style.md` — snake_casing, resource/data/module block ordering, `count`/`for_each`, dynamic blocks, `lifecycle` syntax, `coalesce`/`try` defaults, provider declarations, `terraform.tf` version constraints, and `locals.tf` rules (TFNFR4, 6–13, 25–27, 31–33)
- `references/variables-and-outputs.md` — variable requirements (ordering, naming, types, sensitive data, nullability, deprecation) and output requirements (anti-corruption layer pattern, sensitive outputs, deprecation) (TFNFR14–24, TFFR2, TFNFR29–30)
- `references/breaking-changes.md` — feature-toggle requirements for new resources and the breaking-change review checklist (TFNFR34–35)
- `references/testing-and-docs.md` — required test tooling, test provider config, and terraform-docs generation (TFNFR5, TFNFR36, TFNFR2)
- `references/contribution.md` — GitHub branch protection requirements for module repos (TFNFR3)

---

## Module Cross-Referencing

**Severity:** MUST | **Requirement:** TFFR1

When building Resource or Pattern modules, module owners **MAY** cross-reference other modules. However:

- Modules **MUST** be referenced using HashiCorp Terraform registry reference to a pinned version
  - Example: `source = "Azure/xxx/azurerm"` with `version = "1.2.3"`
- Modules **MUST NOT** use git references (e.g., `git::https://xxx.yyy/xxx.git` or `github.com/xxx/yyy`)
- Modules **MUST NOT** contain references to non-AVM modules

---

## Azure Provider Requirements

**Severity:** MUST | **Requirement:** TFFR3

Authors **MUST** only use the following Azure providers:

| Provider | Min Version | Max Version |
| -------- | ----------- | ----------- |
| azapi    | >= 2.0      | < 3.0       |
| azurerm  | >= 4.0      | < 5.0       |

**Requirements:**

- Authors **MAY** select either Azurerm, Azapi, or both providers
- **MUST** use `required_providers` block to enforce provider versions
- **SHOULD** use pessimistic version constraint operator (`~>`)

**Example:**

```hcl
terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
  }
}
```

---

## Compliance Checklist

Use this checklist when developing or reviewing Azure Verified Modules:

### Module Structure

- [ ] Module cross-references use registry sources with pinned versions
- [ ] Azure providers (azurerm/azapi) versions meet AVM requirements
- [ ] `.terraform-docs.yml` present in module root
- [ ] CODEOWNERS file present

### Code Style

- [ ] All names use lower snake_casing
- [ ] Resources ordered with dependencies first
- [ ] `for_each` uses `map()` or `set()` with static keys
- [ ] Resource/data/module blocks follow proper internal ordering
- [ ] `ignore_changes` not quoted
- [ ] Dynamic blocks used for conditional nested objects
- [ ] `coalesce()` or `try()` used for default values

### Variables

- [ ] No `enabled` or `module_depends_on` variables
- [ ] Variables ordered: required (alphabetical) then optional (alphabetical)
- [ ] All variables have precise types (avoid `any`)
- [ ] All variables have descriptions
- [ ] Collections have `nullable = false`
- [ ] No `sensitive = false` declarations
- [ ] No default values for sensitive inputs
- [ ] Deprecated variables moved to `deprecated_variables.tf`

### Outputs

- [ ] Outputs use anti-corruption layer pattern (discrete attributes)
- [ ] Sensitive outputs marked `sensitive = true`
- [ ] Deprecated outputs moved to `deprecated_outputs.tf`

### Terraform Configuration

- [ ] `terraform.tf` has version constraints (`~>` format)
- [ ] `required_providers` block present with all providers
- [ ] No `provider` declarations in module (except aliases)
- [ ] Locals arranged alphabetically

### Testing & Quality

- [ ] Required testing tools configured
- [ ] New resources have feature toggles
- [ ] Breaking changes reviewed and documented

---

## Summary Statistics

- **Functional Requirements:** 3
- **Non-Functional Requirements:** 34
- **Total Requirements:** 37

### By Severity

- **MUST:** 21 requirements
- **SHOULD:** 14 requirements
- **MAY:** 2 requirements
