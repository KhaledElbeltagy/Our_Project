# Our_Project

## RAW Hazard Assembly Test Generator

The repository contains a Python utility that emits RISC-V assembly focused on
read-after-write (RAW) hazards. It builds small programs where a *consumer*
instruction immediately reads the destination written by a *producer*, allowing
you to stress forwarding paths in a pipeline or detect missing interlocks.

### Usage

```bash
python scripts/generate_raw_hazard_tests.py \
  --config configs/raw_hazard_tests.json \
  --output generated/raw_hazard_tests.S
```

Key flags:

- `--config`: Optional JSON/YAML file that lists the producer/consumer pairs.
  Omitting it falls back to the baked-in sample scenarios.
- `--xlen`: Select `32` (default) or `64` to control the store width.
- `--default-gap`: Number of `nop`s between producer and consumer when the test
  itself does not override the gap.
- `--signature-label`: Symbol name for the signature buffer.

### Config structure

Each entry in `tests` contains:

- `name`: Identifier used to label the test block.
- `comment`: Optional free-form note added as an inline comment.
- `gap`: Overrides the global `default_gap` for a single test.
- `producer` / `consumer`: Instruction objects with:
  - `mnemonic`: Instruction mnemonic, e.g. `add`.
  - `format`: Operand pattern (`R` for rd,rs1,rs2 or `I` for rd,rs1,imm).
  - `dependency_operand`: For consumers, which operand (`rs1` or `rs2`) should
    reuse the producer’s `rd`. Defaults to the first source register.
  - `source_values`: Optional literal values loaded via `li` for specific
    operands (keys match operand names like `rs1`).
  - `immediate`: Optional literal immediate for `I`-type instructions.

Example snippet:

```json
{
  "name": "addi_to_add",
  "producer": {
    "mnemonic": "addi",
    "format": "I",
    "immediate": 8,
    "source_values": { "rs1": 11 }
  },
  "consumer": {
    "mnemonic": "add",
    "format": "R",
    "dependency_operand": "rs1",
    "source_values": { "rs2": 4 }
  }
}
```

Run the script to generate `generated/raw_hazard_tests.S`, then feed that file
into your assembler, ISS, or RTL testbench. The emitted program writes the
consumer results into a `signature_area` buffer so post-processing scripts can
validate architectural state.