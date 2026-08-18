# Breaking Changes & Feature Management

## Using Feature Toggles

**Severity:** MUST | **Requirement:** TFNFR34

New resources added in minor/patch versions **MUST** have a toggle variable to avoid creation by default:

```hcl
variable "create_route_table" {
  type     = bool
  default  = false
  nullable = false
}

resource "azurerm_route_table" "this" {
  count = var.create_route_table ? 1 : 0
  # ...
}
```

## Reviewing Potential Breaking Changes

**Severity:** MUST | **Requirement:** TFNFR35

**Breaking changes requiring caution:**

**Resource blocks:**

1. Adding new resource without conditional creation
2. Adding arguments with non-default values
3. Adding nested blocks without `dynamic`
4. Renaming resources without `moved` blocks
5. Changing `count` to `for_each` or vice versa

**Variable/Output blocks:**

1. Deleting/renaming variables
2. Changing variable `type`
3. Changing variable `default` values
4. Changing `nullable` to false
5. Changing `sensitive` from false to true
6. Adding variables without `default`
7. Deleting outputs
8. Changing output `value`
9. Changing output `sensitive` value
