"""Differential checks for fixed stack frames in the jump-table translator.

Each export of tests/cpu/frames.asm runs through the register probe. The source-built
original must match the Python reference, and the translation must match the original on
the return value, the guarded trace and the probe's RBX and RSI sentinels.
"""

import ctypes
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from direct_check import GUARDS, MASK, SENTINELS, observe, register_probe

MASK32 = 0xffffffff
EXPORTS = ("save_nonvolatile", "spill_reload", "home_spill", "join_frame", "loop_counter")


def trace(first, second):
    return (GUARDS[0], first & MASK, second & MASK, MASK, GUARDS[1])


def save_nonvolatile(x, y):
    rbx = (x * y) & MASK
    rsi = y ^ rbx
    return (rbx + rsi) & MASK, trace(rbx, rsi)


def spill_reload(x, y):
    combined = (x & MASK32) << 32 | (y & MASK32)
    narrow = (y & 0xffff) | (x & 0xff) << 16 | 0xa5 << 24
    return (combined - x) & MASK, trace(combined, narrow)


def home_spill(x, y):
    total = (x + y) & MASK
    return total, trace(total, x)


def join_frame(x, y):
    value, factor = (y, 2) if x & MASK32 < y & MASK32 else (x, 1)
    product = (value * factor) & MASK
    return product, trace(product, y)


def loop_counter(x, y):
    count = y & 15
    total = 0
    for _ in range(count):
        total = (total * 3 + x) & MASK
    return total, trace(count, count)


REFERENCES = {name: globals()[name] for name in EXPORTS}


def inputs():
    """Boundary words for both arguments, plus seeded random pairs."""
    edges = (0, 1, 2, 0x7f, 0x80, 0xff, 0x7fff, 0xffff, 0x7fffffff, 0x80000000, MASK32,
             0x100000000, 0x8000000000000000, MASK, 0x0123456789abcdef)
    rng = random.Random(317)
    pairs = [(x, y) for x in edges for y in edges]
    pairs += [(rng.getrandbits(64), rng.getrandbits(64)) for _ in range(256)]
    # Every loop count from 0 to 15, with the upper bits of the count word set and clear.
    pairs += [(x, count | high) for x in (1, 0xfedcba9876543210) for count in range(16)
              for high in (0, 0xfff0)]
    return pairs


def run(probe, library, prefix=""):
    observed = {}
    for name in EXPORTS:
        function = ctypes.cast(getattr(library, prefix + name), ctypes.c_void_p).value
        observed[name] = [observe(probe, function, x, y) for x, y in inputs()]
    return observed


def verify_reference(observed, label):
    for name in EXPORTS:
        for (x, y), (returned, words, registers) in zip(inputs(), observed[name]):
            want = REFERENCES[name](x, y)
            if (returned, words) != want or registers != SENTINELS:
                raise AssertionError(f"{label} {name}({x:#x}, {y:#x}): {returned:#x} {words} {registers} "
                                     f"!= {want[0]:#x} {want[1]} {SENTINELS}")


def check_native(probe_path, original_path):
    probe = register_probe(probe_path)
    verify_reference(run(probe, ctypes.CDLL(str(original_path))), "source fixture")
    print(f"source fixture: {len(EXPORTS)} exports x {len(inputs())} inputs match the reference")


def check(probe_path, original_path, lifted_path):
    probe = register_probe(probe_path)
    original = run(probe, ctypes.CDLL(str(original_path)))
    verify_reference(original, "source fixture")
    translated = run(probe, ctypes.CDLL(str(lifted_path)), "lifted_")
    for name in EXPORTS:
        for (x, y), want, got in zip(inputs(), original[name], translated[name]):
            if got != want:
                raise AssertionError(f"translation {name}({x:#x}, {y:#x}): {got} != {want}")
    print(f"{len(EXPORTS)} exports x {len(inputs())} inputs: the source fixture matches the reference and the "
          "translation matches it on return, guarded trace, RBX and RSI")


# (export, offset of the instruction from the export, original bytes, patch, expected refusal).
# Each patch keeps the instruction length, so every other instruction stays where it was.
CONTROLS = [
    ("save_nonvolatile", 0x1a, "5e", "5b", "pop of rbx reads another register's save slot"),
    ("save_nonvolatile", 0x1a, "5e", "90", "stack offset -8 at RET"),
    ("save_nonvolatile", 0x02, "48 8b d9", "48 8b cb", "nonvolatile register rbx holds the caller's value"),
    ("spill_reload", 0x09, "89 14 24", "90 90 90", "undefined frame bytes at offset -40"),
    ("spill_reload", 0x39, "48 83 c4 28", "48 8b e0 90", "stack pointer write"),
    ("home_spill", 0x0a, "49 8b c8", "48 8b cc", "frame address escapes through rsp"),
    ("home_spill", 0x0a, "49 8b c8", "49 8b f8", "nonvolatile register rdi is not restored"),
    ("home_spill", 0x0d, "48 8b 44 24 10", "48 8d 44 24 10", "frame address escapes"),
    ("home_spill", 0x0d, "48 8b 44 24 10", "48 8b 44 24 00", "return address slot"),
    ("home_spill", 0x0d, "48 8b 44 24 10", "48 8b 44 24 28", "stack argument accessed"),
    ("home_spill", 0x0d, "48 8b 44 24 10", "48 8b 44 24 f8", "frame access below the stack pointer"),
    # Only the path that takes y loses its store, so the read after the join is undefined on one path.
    ("join_frame", 0x20, "c7 44 24 18 02 00 00 00", "90 " * 7 + "90", "undefined frame bytes at offset -16"),
    ("join_frame", 0x11, "c7 44 24 18 01 00 00 00", "50 " + "90 " * 6 + "90", "stack offset differs at join"),
    ("join_frame", 0x28, "48 8b 44 24 08", "48 8b 44 24 20", "saved register slot read"),
    ("loop_counter", 0x01, "48 83 ec 10", "48 2b e1 90", "stack pointer write"),
]


def check_rejections(original_path):
    import pefile
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    from cpu_leaf_aot import export_rva
    from cpu_switch_aot import translate
    from tempfile import TemporaryDirectory

    data = Path(original_path).read_bytes()
    with pefile.PE(data=data, fast_load=True) as pe:
        locate = {name: pe.get_offset_from_rva(export_rva(pe, name)) for name in EXPORTS}
    with TemporaryDirectory() as folder:
        path = Path(folder) / "control.dll"
        for name, delta, before, after, reason in CONTROLS:
            before, after = bytes.fromhex(before), bytes.fromhex(after)
            at = locate[name] + delta
            if data[at:at + len(before)] != before or len(after) != len(before):
                raise AssertionError(f"unexpected encoding for control {reason!r} in {name}")
            damaged = bytearray(data)
            damaged[at:at + len(after)] = after
            path.write_bytes(damaged)
            try:
                translate(path, [name])
            except ValueError as error:
                if reason not in str(error):
                    raise AssertionError(f"{name} control {reason!r} refused for another reason: {error}") from error
            else:
                raise AssertionError(f"{name} control accepted: {reason}")
            print(f"control {name}+{delta:#x}: {reason}")
    print(f"{len(CONTROLS)} frame rejection controls passed")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "native":
        check_native(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 5 and sys.argv[1] == "check":
        check(*sys.argv[2:])
        check_rejections(sys.argv[3])
    else:
        raise SystemExit("usage: frames_check.py native PROBE ORIGINAL | check PROBE ORIGINAL LIFTED")
