# Variable & Output Requirements

Detailed rules for declaring variables and outputs in an AVM-compliant module.

## Variable Requirements

### Not Allowed Variables

**Severity:** MUST | **Requirement:** TFNFR14

Module owners **MUST NOT** add variables like `enabled` or `module_depends_on` to control entire module operation. Boolean feature toggles for specific resources are acceptable.

### Variable Definition Order

**Severity:** SHOULD | **Requirement:** TFNFR15

Variables **SHOULD** follow this order:

1. All required fields (alphabetical)
2. All optional fields (alphabetical)

### Variable Naming Rules

**Severity:** SHOULD | **Requirement:** TFNFR16

- Follow [HashiCorp's naming rules](https://www.terraform.io/docs/extend/best-practices/naming.html)
- Feature switches **SHOULD** use positive statements: `xxx_enabled` instead of `xxx_disabled`

### Variables with Descriptions

**Severity:** SHOULD | **Requirement:** TFNFR17

- `description` **SHOULD** precisely describe the parameter's purpose and expected data type
- Target audience is module users, not developers
- For `object` types, use HEREDOC format

### Variables with Types

**Severity:** MUST | **Requirement:** TFNFR18

- `type` **MUST** be defined for every variable
- `type` **SHOULD** be as precise as possible
- `any` **MAY** only be used with adequate reasons
- Use `bool` instead of `string`/`number` for true/false values
- Use concrete `object` instead of `map(any)`

### Sensitive Data Variables

**Severity:** SHOULD | **Requirement:** TFNFR19

If a variable's type is `object` and contains sensitive fields, the entire variable **SHOULD** be `sensitive = true`, or extract sensitive fields into separate variables.

### Non-Nullable Defaults for Collections

**Severity:** SHOULD | **Requirement:** TFNFR20

Nullable **SHOULD** be set to `false` for collection values (sets, maps, lists) when using them in loops. For scalar values, null may have semantic meaning.

### Discourage Nullability by Default

**Severity:** MUST | **Requirement:** TFNFR21

`nullable = true` **MUST** be avoided unless there's a specific semantic need for null values.

### Avoid sensitive = false

**Severity:** MUST | **Requirement:** TFNFR22

`sensitive = false` **MUST** be avoided (this is the default).

### Sensitive Default Value Conditions

**Severity:** MUST | **Requirement:** TFNFR23

A default value **MUST NOT** be set for sensitive inputs (e.g., default passwords).

### Handling Deprecated Variables

**Severity:** MUST | **Requirement:** TFNFR24

- Move deprecated variables to `deprecated_variables.tf`
- Annotate with `DEPRECATED` at the beginning of description
- Declare the replacement's name
- Clean up during major version releases

## Output Requirements

### Additional Terraform Outputs

**Severity:** SHOULD | **Requirement:** TFFR2

Authors **SHOULD NOT** output entire resource objects as these may contain sensitive data and the schema can change with API or provider versions.

**Best Practices:**

- Output *computed* attributes of resources as discrete outputs (anti-corruption layer pattern)
- **SHOULD NOT** output values that are already inputs (except `name`)
- Use `sensitive = true` for sensitive attributes
- For resources deployed with `for_each`, output computed attributes in a map structure

**Examples:**

```hcl
# Single resource computed attribute
output "foo" {
  description = "MyResource foo attribute"
  value       = azurerm_resource_myresource.foo
}

# for_each resources
output "childresource_foos" {
  description = "MyResource children's foo attributes"
  value = {
    for key, value in azurerm_resource_mychildresource : key => value.foo
  }
}

# Sensitive output
output "bar" {
  description = "MyResource bar attribute"
  value       = azurerm_resource_myresource.bar
  sensitive   = true
}
```

### Sensitive Data Outputs

**Severity:** MUST | **Requirement:** TFNFR29

Outputs containing confidential data **MUST** be declared with `sensitive = true`.

### Handling Deprecated Outputs

**Severity:** MUST | **Requirement:** TFNFR30

- Move deprecated outputs to `deprecated_outputs.tf`
- Define new outputs in `outputs.tf`
- Clean up during major version releases
