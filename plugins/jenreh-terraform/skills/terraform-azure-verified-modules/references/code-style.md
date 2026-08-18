# Code Style Standards

Detailed formatting, ordering, and syntax rules for AVM-compliant Terraform code.

## Lower snake_casing

**Severity:** MUST | **Requirement:** TFNFR4

**MUST** use lower snake_casing for:

- Locals
- Variables
- Outputs
- Resources (symbolic names)
- Modules (symbolic names)

Example: `snake_casing_example`

## Resource & Data Source Ordering

**Severity:** SHOULD | **Requirement:** TFNFR6

- Resources that are depended on **SHOULD** come first
- Resources with dependencies **SHOULD** be defined close to each other

## Count & for_each Usage

**Severity:** MUST | **Requirement:** TFNFR7

- Use `count` for conditional resource creation
- **MUST** use `map(xxx)` or `set(xxx)` as resource's `for_each` collection
- The map's key or set's element **MUST** be static literals

**Example:**

```hcl
resource "azurerm_subnet" "pair" {
  for_each             = var.subnet_map  # map(string)
  name                 = "${each.value}-pair"
  resource_group_name  = azurerm_resource_group.example.name
  virtual_network_name = azurerm_virtual_network.example.name
  address_prefixes     = ["10.0.1.0/24"]
}
```

## Resource & Data Block Internal Ordering

**Severity:** SHOULD | **Requirement:** TFNFR8

**Order within resource/data blocks:**

1. **Meta-arguments (top)**:
   - `provider`
   - `count`
   - `for_each`

2. **Arguments/blocks (middle, alphabetical)**:
   - Required arguments
   - Optional arguments
   - Required nested blocks
   - Optional nested blocks

3. **Meta-arguments (bottom)**:
   - `depends_on`
   - `lifecycle` (with sub-order: `create_before_destroy`, `ignore_changes`, `prevent_destroy`)

Separate sections with blank lines.

## Module Block Ordering

**Severity:** SHOULD | **Requirement:** TFNFR9

**Order within module blocks:**

1. **Top meta-arguments**:
   - `source`
   - `version`
   - `count`
   - `for_each`

2. **Arguments (alphabetical)**:
   - Required arguments
   - Optional arguments

3. **Bottom meta-arguments**:
   - `depends_on`
   - `providers`

## Lifecycle ignore_changes Syntax

**Severity:** MUST | **Requirement:** TFNFR10

The `ignore_changes` attribute **MUST NOT** be enclosed in double quotes.

**Good:**

```hcl
lifecycle {
  ignore_changes = [tags]
}
```

**Bad:**

```hcl
lifecycle {
  ignore_changes = ["tags"]
}
```

## Null Comparison for Conditional Creation

**Severity:** SHOULD | **Requirement:** TFNFR11

For parameters requiring conditional resource creation, wrap with `object` type to avoid "known after apply" issues during plan stage.

**Recommended:**

```hcl
variable "security_group" {
  type = object({
    id = string
  })
  default = null
}
```

## Dynamic Blocks for Optional Nested Objects

**Severity:** MUST | **Requirement:** TFNFR12

Nested blocks under conditions **MUST** use this pattern:

```hcl
dynamic "identity" {
  for_each = <condition> ? [<some_item>] : []

  content {
    # block content
  }
}
```

## Default Values with coalesce/try

**Severity:** SHOULD | **Requirement:** TFNFR13

**Good:**

```hcl
coalesce(var.new_network_security_group_name, "${var.subnet_name}-nsg")
```

**Bad:**

```hcl
var.new_network_security_group_name == null ? "${var.subnet_name}-nsg" : var.new_network_security_group_name
```

## Provider Declarations in Modules

**Severity:** MUST | **Requirement:** TFNFR27

- `provider` **MUST NOT** be declared in modules (except for `configuration_aliases`)
- `provider` blocks in modules **MUST** only use `alias`
- Provider configurations **SHOULD** be passed in by module users

## Terraform Version Requirements

**Severity:** MUST | **Requirement:** TFNFR25

**`terraform.tf` requirements:**

- **MUST** contain only one `terraform` block
- First line **MUST** define `required_version`
- **MUST** include minimum version constraint
- **MUST** include maximum major version constraint
- **SHOULD** use `~> #.#` or `>= #.#.#, < #.#.#` format

**Example:**

```hcl
terraform {
  required_version = "~> 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}
```

## Providers in required_providers

**Severity:** MUST | **Requirement:** TFNFR26

- `terraform` block **MUST** contain `required_providers` block
- Each provider **MUST** specify `source` and `version`
- Providers **SHOULD** be sorted alphabetically
- Only include directly required providers
- `source` **MUST** be in format `namespace/name`
- `version` **MUST** include minimum and maximum major version constraints
- **SHOULD** use `~> #.#` or `>= #.#.#, < #.#.#` format

## Local Values Standards

### locals.tf Organization

**Severity:** MAY | **Requirement:** TFNFR31

- `locals.tf` **SHOULD** only contain `locals` blocks
- **MAY** declare `locals` blocks next to resources for advanced scenarios

### Alphabetical Local Arrangement

**Severity:** MUST | **Requirement:** TFNFR32

Expressions in `locals` blocks **MUST** be arranged alphabetically.

### Precise Local Types

**Severity:** SHOULD | **Requirement:** TFNFR33

Use precise types (e.g., `number` for age, not `string`).
