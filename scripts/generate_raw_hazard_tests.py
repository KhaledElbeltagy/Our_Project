#!/usr/bin/env python3
"""
Generate RISC-V assembly tests that exercise read-after-write (RAW) hazards.

Each test pairs a producer instruction with a consumer instruction that uses
the freshly produced destination register without any intervening scoreboard
logic. The resulting assembly can be used to stress pipeline forwarding logic
in simulators or hardware implementations.

The generator relies on a small JSON (or YAML, if PyYAML is installed) config
file describing the instruction pairs you want to cover. See the accompanying
`configs/raw_hazard_tests.json` file for an example.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - PyYAML is optional
    yaml = None


SUPPORTED_FORMATS = {"R", "I"}

REGISTER_INPUTS = {
    "R": ("rs1", "rs2"),
    "I": ("rs1",),
}

REGISTER_OUTPUTS = {
    "R": ("rd",),
    "I": ("rd",),
}

FORMAT_TEMPLATES = {
    "R": "{mnemonic} {rd}, {rs1}, {rs2}",
    "I": "{mnemonic} {rd}, {rs1}, {imm}",
}

DEFAULT_CONFIG = {
    "isa": "rv32i",
    "default_gap": 0,
    "tests": [
        {
            "name": "add_result_into_sub",
            "comment": "SUB consumes ADD result through rs1",
            "producer": {"mnemonic": "add", "format": "R"},
            "consumer": {"mnemonic": "sub", "format": "R", "dependency_operand": "rs1"},
        },
        {
            "name": "mul_forward_into_xor",
            "comment": "XOR reads MUL output via rs2 with a one cycle gap",
            "gap": 1,
            "producer": {"mnemonic": "mul", "format": "R"},
            "consumer": {"mnemonic": "xor", "format": "R", "dependency_operand": "rs2"},
        },
        {
            "name": "addi_then_add",
            "comment": "Integer immediate op feeds an R-type consumer",
            "producer": {"mnemonic": "addi", "format": "I", "immediate": 4},
            "consumer": {"mnemonic": "add", "format": "R", "dependency_operand": "rs1"},
        },
    ],
}

SOURCE_POOL = (
    "x5",
    "x6",
    "x7",
    "x8",
    "x9",
    "x18",
    "x19",
    "x20",
    "x21",
    "x22",
    "x23",
    "x24",
    "x25",
    "x26",
    "x27",
    "x28",
    "x29",
    "x30",
)

DEST_POOL = ("x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17")

SIGNATURE_POINTER = "x31"


class ConfigError(Exception):
    """Raised when the user-provided configuration is invalid."""


@dataclass
class InstructionConfig:
    mnemonic: str
    fmt: str
    dependency_operand: Optional[str] = None
    source_values: Dict[str, int] = field(default_factory=dict)
    immediate: Optional[int] = None

    @classmethod
    def from_mapping(cls, data: Dict[str, object], role: str) -> "InstructionConfig":
        try:
            mnemonic = str(data["mnemonic"])
            fmt = str(data["format"]).upper()
        except KeyError as exc:
            raise ConfigError(f"{role} instruction is missing required key: {exc}") from exc

        if fmt not in SUPPORTED_FORMATS:
            raise ConfigError(
                f"{role} instruction '{mnemonic}' uses unsupported format '{fmt}'. "
                f"Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}"
            )

        dependency_operand = data.get("dependency_operand")
        if dependency_operand is not None:
            dependency_operand = str(dependency_operand)

        source_values = {
            key: int(value)
            for key, value in (data.get("source_values") or {}).items()
        }

        immediate = data.get("immediate")
        if immediate is not None:
            immediate = int(immediate)

        return cls(
            mnemonic=mnemonic,
            fmt=fmt,
            dependency_operand=dependency_operand,
            source_values=source_values,
            immediate=immediate,
        )


@dataclass
class TestConfig:
    name: str
    producer: InstructionConfig
    consumer: InstructionConfig
    gap: Optional[int] = None
    comment: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Dict[str, object]) -> "TestConfig":
        try:
            name = str(data["name"])
        except KeyError as exc:
            raise ConfigError("Test is missing required key 'name'") from exc

        comment = data.get("comment")
        if comment is not None:
            comment = str(comment)

        gap = data.get("gap")
        if gap is not None:
            gap = int(gap)
            if gap < 0:
                raise ConfigError(f"Test '{name}' gap must be non-negative")

        producer = InstructionConfig.from_mapping(
            data.get("producer") or {},
            role=f"Test '{name}' producer",
        )
        consumer = InstructionConfig.from_mapping(
            data.get("consumer") or {},
            role=f"Test '{name}' consumer",
        )

        return cls(
            name=name,
            producer=producer,
            consumer=consumer,
            gap=gap,
            comment=comment,
        )


class ValueAllocator:
    """Generate deterministic register initialisation values."""

    def __init__(self, start: int = 5, step: int = 7, limit: int = 0x3FF):
        self.current = start
        self.step = step
        self.limit = limit

    def next(self) -> int:
        value = self.current
        self.current += self.step
        if self.current > self.limit:
            self.current = (self.current % self.limit) + self.step
        return value


class RawHazardAssemblyBuilder:
    def __init__(
        self,
        tests: List[TestConfig],
        xlen: int,
        default_gap: int,
        signature_label: str,
    ) -> None:
        if xlen not in (32, 64):
            raise ValueError("xlen must be either 32 or 64")

        self.tests = tests
        self.xlen = xlen
        self.word_size = 4 if xlen == 32 else 8
        self.store_instruction = "sw" if xlen == 32 else "sd"
        self.word_directive = ".word" if xlen == 32 else ".dword"
        self.default_gap = max(default_gap, 0)
        self.signature_label = signature_label
        self.value_allocator = ValueAllocator()

    def build(self) -> str:
        text_lines: List[str] = [
            "# Auto-generated RAW hazard tests",
            f"# XLEN = {self.xlen}",
            "",
            "    .section .text",
            "    .globl _start",
            "_start:",
            f"    la {SIGNATURE_POINTER}, {self.signature_label}",
            "",
        ]

        for index, test in enumerate(self.tests):
            text_lines.extend(self._emit_test(index, test))

        text_lines.extend(
            [
                "end_of_tests:",
                "    ebreak",
                "    j end_of_tests",
                "",
                "    .section .data",
                f"    .balign {self.word_size}",
                f"{self.signature_label}:",
                f"    .space {self.word_size * max(1, len(self.tests))}",
                "",
            ]
        )

        return "\n".join(text_lines)

    def _emit_test(self, index: int, test: TestConfig) -> List[str]:
        producer_regs = self._assign_registers(test.producer.fmt, index, role="producer")
        consumer_regs = self._assign_registers(test.consumer.fmt, index, role="consumer")

        dependency_operand = self._resolve_dependency_operand(test)
        consumer_regs[dependency_operand] = producer_regs["rd"]

        if "rd" in consumer_regs and consumer_regs["rd"] == producer_regs["rd"]:
            consumer_regs["rd"] = self._alternate_dest(consumer_regs["rd"], index)

        setup_values = self._collect_setup_values(
            producer_regs,
            test.producer,
            consumer_regs,
            test.consumer,
            dependency_operand,
        )

        setup_lines = [
            f"    li {reg}, {value}"
            for reg, value in setup_values
        ]

        producer_line = self._format_instruction(
            test.producer,
            producer_regs,
        )
        consumer_line = self._format_instruction(
            test.consumer,
            consumer_regs,
        )

        gap = test.gap if test.gap is not None else self.default_gap

        test_label = f"test_{index:02d}_{test.name}"
        lines: List[str] = [f"{test_label}:"]
        if test.comment:
            lines.append(f"    # {test.comment}")
        lines.extend(setup_lines)
        lines.append(f"    {producer_line}")
        for _ in range(gap):
            lines.append("    nop")
        lines.append(f"    {consumer_line}")
        consumer_dest = consumer_regs.get("rd")
        if consumer_dest is None:
            raise ConfigError(
                f"Consumer instruction '{test.consumer.mnemonic}' must provide an 'rd' register."
            )
        lines.append(
            f"    {self.store_instruction} {consumer_dest}, 0({SIGNATURE_POINTER})"
        )
        lines.append(
            f"    addi {SIGNATURE_POINTER}, {SIGNATURE_POINTER}, {self.word_size}"
        )
        lines.append("")
        return lines

    def _resolve_dependency_operand(self, test: TestConfig) -> str:
        candidate = test.consumer.dependency_operand
        if candidate is None:
            candidate = REGISTER_INPUTS[test.consumer.fmt][0]

        candidate = candidate.lower()
        if candidate not in REGISTER_INPUTS[test.consumer.fmt]:
            raise ConfigError(
                f"Consumer '{test.consumer.mnemonic}' cannot depend on operand '{candidate}'"
            )
        return candidate

    def _collect_setup_values(
        self,
        producer_regs: Dict[str, str],
        producer_cfg: InstructionConfig,
        consumer_regs: Dict[str, str],
        consumer_cfg: InstructionConfig,
        dependency_operand: str,
    ) -> List[Tuple[str, int]]:
        ordered_regs: List[str] = []
        reg_value_map: Dict[str, int] = {}

        for operand in REGISTER_INPUTS[producer_cfg.fmt]:
            reg = producer_regs[operand]
            ordered_regs.append(reg)
            override = producer_cfg.source_values.get(operand)
            reg_value_map[reg] = override if override is not None else self.value_allocator.next()

        for operand in REGISTER_INPUTS[consumer_cfg.fmt]:
            if operand == dependency_operand:
                continue
            reg = consumer_regs[operand]
            if reg in reg_value_map:
                continue
            ordered_regs.append(reg)
            override = consumer_cfg.source_values.get(operand)
            reg_value_map[reg] = override if override is not None else self.value_allocator.next()

        seen = set()
        result: List[Tuple[str, int]] = []
        for reg in ordered_regs:
            if reg in seen:
                continue
            seen.add(reg)
            result.append((reg, reg_value_map[reg]))
        return result

    def _format_instruction(
        self,
        config: InstructionConfig,
        regs: Dict[str, str],
    ) -> str:
        if config.fmt == "I":
            imm_value = config.immediate
            if imm_value is None:
                imm_value = self.value_allocator.next()
            # Keep immediate within 12-bit signed range.
            imm_value = ((imm_value + 0x800) & 0xFFF) - 0x800
            template_args = {**regs, "mnemonic": config.mnemonic, "imm": imm_value}
        else:
            template_args = {**regs, "mnemonic": config.mnemonic}

        template = FORMAT_TEMPLATES[config.fmt]
        return template.format(**template_args)

    def _assign_registers(
        self, fmt: str, index: int, role: str
    ) -> Dict[str, str]:
        regs: Dict[str, str] = {}
        dest_offset = 0 if role == "producer" else len(DEST_POOL) // 2
        if "rd" in REGISTER_OUTPUTS[fmt]:
            regs["rd"] = self._pick_from_pool(DEST_POOL, index + dest_offset)

        source_offset = 0 if role == "producer" else len(SOURCE_POOL) // 3
        inputs = REGISTER_INPUTS[fmt]
        for idx, operand in enumerate(inputs):
            regs[operand] = self._pick_from_pool(
                SOURCE_POOL,
                index * len(inputs) + idx + source_offset,
            )
        return regs

    @staticmethod
    def _pick_from_pool(pool: Iterable[str], index: int) -> str:
        pool_list = list(pool)
        if not pool_list:
            raise ConfigError("Register pool is empty.")
        return pool_list[index % len(pool_list)]

    def _alternate_dest(self, current: str, index: int) -> str:
        for offset in range(1, len(DEST_POOL) + 1):
            candidate = self._pick_from_pool(DEST_POOL, index + offset)
            if candidate != current:
                return candidate
        raise ConfigError("Unable to allocate alternate destination register.")


def load_structure(path: Optional[pathlib.Path]) -> Dict[str, object]:
    if path is None:
        return DEFAULT_CONFIG

    if not path.exists():
        raise ConfigError(f"Config file '{path}' does not exist.")

    data = path.read_text()
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise ConfigError(
                "PyYAML is required to parse YAML configs. Install it or use JSON."
            )
        return yaml.safe_load(data)
    return json.loads(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate RISC-V RAW hazard assembly tests.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=pathlib.Path,
        help="Path to a JSON (or YAML) config file. Defaults to built-in scenarios.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=pathlib.Path,
        default=pathlib.Path("generated/raw_hazard_tests.S"),
        help="Output assembly file path.",
    )
    parser.add_argument(
        "--xlen",
        type=int,
        choices=(32, 64),
        default=32,
        help="Target XLEN (32 or 64).",
    )
    parser.add_argument(
        "--signature-label",
        type=str,
        default="signature_area",
        help="Label used for the signature buffer.",
    )
    parser.add_argument(
        "--default-gap",
        type=int,
        default=0,
        help="Number of NOPs inserted between producer and consumer when a test does not override it.",
    )
    return parser.parse_args()


def ensure_parent_directory(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def main() -> None:
    args = parse_args()
    try:
        raw_config = load_structure(args.config)
        tests = [
            TestConfig.from_mapping(item)
            for item in raw_config.get("tests", [])
        ]
        if not tests:
            raise ConfigError("Config must contain at least one test definition.")
        isa = raw_config.get("isa", f"rv{args.xlen}i")
        default_gap = raw_config.get("default_gap", args.default_gap)
        builder = RawHazardAssemblyBuilder(
            tests=tests,
            xlen=args.xlen,
            default_gap=int(default_gap),
            signature_label=args.signature_label,
        )
        assembly = builder.build()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    ensure_parent_directory(args.output)
    args.output.write_text(assembly)
    print(
        f"Wrote {len(tests)} RAW hazard tests for {isa.upper()} to {args.output}"
    )


if __name__ == "__main__":
    main()
