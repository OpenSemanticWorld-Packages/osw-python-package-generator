# osw-python-package-generator
Generates pydantic dataclass packages from OO-LD schemas

## Versioning policy

Generated Python packages are versioned based on the underlying schema package and the generator script.

### Base version: schema package

The base version is always taken from the schema package tag, e.g.:

- Schema package tag: `0.53.0` (or `v0.53.0`)
- Python package base version: `0.53.0` (or `v0.53.0`)

This ensures you can always see which schema release the generated models were built from.

### Post-release suffix: generator + build

The final Python package version is constructed as:

```text
.post
```

where each component is zero-padded to 3 digits:

- `AAA` – generator script **major** version
- `BBB` – generator script **minor** version
- `CCC` – generator script **patch** version
- `RRR` – **run number** (build counter for the same schema + generator)

These values come from:

- `script_version = "X.Y.Z"` → `AAA = X`, `BBB = Y`, `CCC = Z`
- `run_number` → `RRR` (currently `0`)

Example:

- `schema_version = 0.53.0`
- `script_version = 0.1.1`
- `run_number = 0`

Resulting Python package version:

```text
0.53.0.post000001001000
```

This is PEP 440–compatible (`.post`) and encodes:

- Which schema release was used (`0.53.0`)
- Which generator version produced the code (`0.1.1`)
- Which build/run of that combination it is (`000`)

### Git tags

When `commit=True` is used:

- The generated code is committed to the target repository.
- A Git tag is created with **exactly** the same string as the Python package version (e.g. `0.53.0.post000001001000`).

This keeps Git history, Python distributions, and schema/generator versions aligned.
