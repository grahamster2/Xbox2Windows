"""Recompile bounded Windows x64 leaf functions with jump-table dispatch to C++.

Each export takes three integer words in RCX, RDX and R8 and returns RAX. The
translator follows branches and jump-table entries from the export, so data
placed after the code (as MSVC does with its tables) is never decoded. An
indirect JMP is accepted only at the end of one of these sequences, reached by
the fallthrough of an unsigned bound check on the index:

    mov E32, [B+I*4+table]; add E, B; jmp E   B = image base (MSVC)
    movsxd E, [B+I*4];      add E, B; jmp E   B = table      (LLVM)

B must hold the same RIP-relative address on every path, and the index must be
shown to carry the compared value. The generated C++ replaces the dispatch with
a switch over the table targets.

An export listed as "name:4" also takes a fourth integer word in R9: R9 is then
defined at entry and the generated function has a fourth parameter. "name" and
"name:3" take three, and R9 is a scratch register that must be written before it
is read; any other count is rejected. The count is chosen by whoever invokes the
translator, not derived from the machine code, and the host must call the
translated function with the same number of arguments. Stack arguments (the
fifth and later) and floating-point arguments are not supported.

Flags are modeled only between a producer and its consumers (Jcc, SETcc,
CMOVcc) in one straight-line run that no edge enters. Instructions that leave
the flags unchanged (moves, LEA, NOT, SETcc, CMOVcc) may sit between them.

A forward pass records how many low bytes (0, 1, 2 or 8) of each register every
path defines. A read needs as many defined bytes as it uses, and an 8- or 16-bit
write keeps the other bytes of its register. The return width is the number of
consecutive low RAX bytes that are definitely defined at every reachable RET or
RET 0: 0, 1, 2 or 8, the lower bound over joins and over returns. It counts
defined bytes and does not establish a source-language type. It is None when no
RET is reachable, which describes the reachable control flow and makes no
termination claim. The generated source states the width unless it is 8.

A function may keep a fixed stack frame. RSP is tracked as a constant offset from
its entry value at every instruction; only PUSH and POP of a 64-bit register and
SUB or ADD of RSP with an immediate change it, paths must meet with the same
offset, and every RET must see offset 0. [RSP+disp] loads and stores become
offsets into one byte array per invocation. Accessed bytes must lie at or above
the current RSP, below the return address or in the caller's 32-byte home area
above it; the return-address slot, stack arguments, RSP as an index and any
other use of a frame address (LEA, a register copy of RSP) are refused. Frame
bytes are defined per path like registers: a read needs every byte it reads
written on every incoming path, and the home area starts undefined.

The nonvolatile registers (RBX, RBP, RSI, RDI, R12-R15) start holding the
caller's value, which the translation does not reproduce. A PUSH of such a
register saves it, and the POP of that slot into the same register restores it.
Reading a caller's value other than to save it is refused, as is a read of a
saved slot other than its restore, and every RET must see each nonvolatile
register restored. Calls remain refused, so no frame address crosses a call.
"""

import argparse
from pathlib import Path
import struct

import pefile
from iced_x86 import (ConditionCode as CC, Decoder, FlowControl, InstructionInfoFactory, MemorySizeExt,
                      Mnemonic as M, OpAccess, OpKind, Register as R, RegisterExt)

from cpu_leaf_aot import export_rva
from cpu_ranges import function_ranges, image_bytes


NAMES = {R.RAX: "rax", R.RCX: "rcx", R.RDX: "rdx", R.RBX: "rbx", R.RSP: "rsp", R.RBP: "rbp",
         R.RSI: "rsi", R.RDI: "rdi", R.R8: "r8", R.R9: "r9", R.R10: "r10", R.R11: "r11",
         R.R12: "r12", R.R13: "r13", R.R14: "r14", R.R15: "r15"}
# Integer argument registers in order. An entry takes the first three unless it is listed as "name:4".
ARGUMENT_REGISTERS = (R.RCX, R.RDX, R.R8, R.R9)
ARGUMENT_COUNTS = (3, 4)
DEFAULT_ARGUMENTS = 3
VOLATILE = {R.RAX, R.RCX, R.RDX, R.R8, R.R9, R.R10, R.R11}
NONVOLATILE = set(NAMES) - VOLATILE - {R.RSP}
# Offsets from entry RSP: the return address occupies [0, 8) and the caller's home area [8, 40).
HOME_BEGIN, HOME_END = 8, 40
MAX_FRAME = 1 << 16
HIGH_BYTES = {R.AH, R.BH, R.CH, R.DH}
ALU = {M.ADD: "+", M.SUB: "-", M.AND: "&", M.OR: "|", M.XOR: "^"}
SHIFTS = {M.SHL: "<<", M.SHR: ">>", M.SAR: ">>"}
_CODES = "O NO B AE E NE BE A S NS P NP L GE LE G".split()
SETCC = {getattr(M, "SET" + code) for code in _CODES}
CMOVCC = {getattr(M, "CMOV" + code) for code in _CODES}
# Instructions that neither read nor write the arithmetic flags, besides SETcc and CMOVcc,
# which only read them.
FLAG_NEUTRAL = {M.MOV, M.MOVZX, M.MOVSX, M.MOVSXD, M.LEA, M.NOP, M.NOT, *SETCC, *CMOVCC}
FLAG_PRODUCERS = {*ALU, M.CMP, M.TEST, M.INC, M.DEC}
MAX_FLAG_DISTANCE = 16
CONDITIONS = {CC.E: "e", CC.NE: "ne", CC.B: "b", CC.AE: "ae", CC.BE: "be", CC.A: "a",
              CC.L: "l", CC.GE: "ge", CC.LE: "le", CC.G: "g", CC.S: "s", CC.NS: "ns"}
MAX_CASES = 1024
MAX_INSTRUCTIONS = 4096


def unsigned(width):
    return f"std::uint{width * 8}_t"


def to_signed(value, bits=64):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def signed(width):
    return f"std::int{width * 8}_t"


def full(register):
    full_register = RegisterExt.full_register(register)
    if full_register not in NAMES:
        raise ValueError(f"unsupported register {register}")
    return full_register


def runtime_functions(pe):
    table = pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
    if not table.Size:
        return []
    if table.Size % 12:
        raise ValueError("incomplete runtime-function record")
    return [(begin, end) for begin, end, _ in
            struct.iter_unpack("<III", image_bytes(pe, table.VirtualAddress, table.Size))]


def owned_ranges(pe, entry):
    """Unwind-owned intervals, or the export's section for a leaf without unwind data."""
    records = runtime_functions(pe)
    if any(begin <= entry < end for begin, end in records):
        return function_ranges(pe, entry), []
    # Windows x64 leaf functions may omit .pdata. Such a function may use any
    # file-backed bytes of its section that no runtime-function entry owns.
    section = pe.get_section_by_rva(entry)
    if not section or not section.Characteristics & 0x20000000:
        raise ValueError(f"export at RVA {entry:#x} is not in an executable section")
    end = section.VirtualAddress + min(section.Misc_VirtualSize, section.SizeOfRawData)
    return [(section.VirtualAddress, end)], records


class Function:
    def __init__(self, pe, entry):
        self.pe = pe
        self.entry = entry
        ranges, self.foreign = owned_ranges(pe, entry)
        self.ranges = [(begin, end, image_bytes(pe, begin, end - begin, executable=True))
                       for begin, end in ranges]
        self.instructions = {}
        self.owner = {}
        self.ending = {}
        self.successors = {}
        self.dispatches = {}

    def decode(self, ip):
        for begin, end, data in self.ranges:
            if begin <= ip < end:
                ins = Decoder(64, data[ip - begin:], ip=ip).decode()
                if ins.is_invalid or ins.next_ip > end:
                    raise ValueError(f"invalid or truncated instruction at RVA {ip:#x}")
                if any(ip < stop and start < ins.next_ip for start, stop in self.foreign):
                    raise ValueError(f"edge outside owned ranges at RVA {ip:#x}: another function's unwind interval")
                return ins
        raise ValueError(f"edge outside owned ranges at RVA {ip:#x}")

    def claim(self, begin, end, owner, what):
        for address in range(begin, end):
            if self.owner.get(address, owner) != owner:
                raise ValueError(f"{what} at RVA {begin:#x} overlaps RVA {self.owner[address]:#x}")
        for address in range(begin, end):
            self.owner[address] = owner

    def explore(self):
        pending = [self.entry]
        indirect = []
        while pending or indirect:
            if not pending:
                ip = indirect.pop()
                pending.extend(self.dispatch(ip))
                continue
            ip = pending.pop()
            if ip in self.instructions:
                continue
            if self.owner.get(ip, ip) != ip:
                raise ValueError(f"edge at RVA {ip:#x} overlaps an instruction")
            if len(self.instructions) >= MAX_INSTRUCTIONS:
                raise ValueError(f"instruction limit reached at RVA {ip:#x}")
            ins = self.decode(ip)
            self.claim(ip, ins.next_ip, ip, "instruction")
            self.instructions[ip] = ins
            self.ending[ins.next_ip] = ins
            flow = ins.flow_control
            if flow == FlowControl.NEXT:
                targets = [ins.next_ip]
            elif flow == FlowControl.CONDITIONAL_BRANCH and ins.is_jcc_short_or_near:
                targets = [ins.near_branch_target, ins.next_ip]
            elif flow == FlowControl.UNCONDITIONAL_BRANCH and ins.is_jmp_short_or_near:
                targets = [ins.near_branch_target]
            elif flow == FlowControl.RETURN and ins.mnemonic == M.RET and (ins.op_count == 0 or ins.immediate(0) == 0):
                targets = []
            elif flow == FlowControl.INDIRECT_BRANCH and ins.op0_kind == OpKind.REGISTER:
                targets = []
                indirect.append(ip)
            else:
                raise ValueError(f"unsupported control flow at RVA {ip:#x}: {ins}")
            self.successors[ip] = targets
            pending.extend(reversed(targets))
        edges = {target for ip, targets in self.successors.items() for target in targets
                 if target != self.instructions[ip].next_ip
                 or self.instructions[ip].flow_control not in (FlowControl.NEXT, FlowControl.CONDITIONAL_BRANCH)}
        constants = self.constants()
        for ip in self.dispatches:
            self.prove(ip, edges, constants)
        return edges

    def previous(self, ins, what):
        before = self.ending.get(ins.ip)
        if before is None or before.flow_control != FlowControl.NEXT:
            raise ValueError(f"missing {what} before RVA {ins.ip:#x}")
        return before

    def constants(self):
        """RIP-relative addresses that every explored path leaves in a register, per instruction."""
        state = {self.entry: {}}
        work = [self.entry]
        while work:
            ip = work.pop()
            ins = self.instructions.get(ip)
            if ins is None:
                continue
            known = dict(state[ip])
            if ip in self.dispatches:
                known.pop(self.dispatches[ip][4], None)
            elif ins.mnemonic == M.LEA and ins.is_ip_rel_memory_operand and RegisterExt.is_gpr64(ins.op0_register):
                known[ins.op0_register] = ins.ip_rel_memory_address
            elif ins.op_count and ins.op0_kind == OpKind.REGISTER and ins.mnemonic not in (M.CMP, M.TEST):
                known.pop(full(ins.op0_register), None)
            for successor in self.successors.get(ip, ()):
                if successor not in self.instructions:
                    continue
                if successor == self.entry:
                    merged = {}
                elif successor in state:
                    merged = {r: v for r, v in state[successor].items() if known.get(r) == v}
                else:
                    merged = known
                if successor not in state or merged != state[successor]:
                    state[successor] = merged
                    work.append(successor)
        return state

    def dispatch(self, ip):
        jump = self.instructions[ip]
        add = self.previous(jump, "table address add")
        load = self.previous(add, "table entry load")
        target = full(jump.op0_register)
        base = add.op1_register if add.op1_kind == OpKind.REGISTER else None
        if (add.mnemonic != M.ADD or add.op0_kind != OpKind.REGISTER or add.op0_register != target
                or not RegisterExt.is_gpr64(target) or base is None or not RegisterExt.is_gpr64(base)
                or base == target):
            raise ValueError(f"unrecognized indirect jump at RVA {ip:#x}")
        index = load.memory_index
        if (load.op1_kind != OpKind.MEMORY or load.memory_base != base or load.memory_index_scale != 4
                or not RegisterExt.is_gpr64(index) or index == base or load.has_segment_prefix
                or full(load.op0_register) != target):
            raise ValueError(f"unrecognized table load at RVA {load.ip:#x}")
        anchor = self.constants().get(load.ip, {}).get(base)
        if anchor is None:
            raise ValueError(f"table base {NAMES[base]} is not a RIP-relative address at RVA {load.ip:#x}")
        if load.mnemonic == M.MOV and RegisterExt.is_gpr32(load.op0_register):
            # Image-relative entries: the base register holds the image base.
            table, kind = load.memory_displacement, "image"
            if anchor != 0 or not 0 < table < 1 << 31:
                raise ValueError(f"image-relative table without an image base at RVA {load.ip:#x}")
        elif load.mnemonic == M.MOVSXD and load.memory_displacement == 0:
            table, kind = anchor, "relative"
        else:
            raise ValueError(f"unrecognized table load at RVA {load.ip:#x}")

        # The bound check must fall through into the dispatch block.
        bound = load
        between = []
        while True:
            bound = self.ending.get(bound.ip)
            if bound is None:
                raise ValueError(f"unbounded jump-table index at RVA {ip:#x}")
            if bound.flow_control == FlowControl.CONDITIONAL_BRANCH:
                break
            if bound.flow_control != FlowControl.NEXT or len(between) >= 8:
                raise ValueError(f"unbounded jump-table index at RVA {ip:#x}")
            between.append(bound)
        compare = self.ending.get(bound.ip)
        if bound.condition_code not in (CC.A, CC.AE):
            raise ValueError(f"jump-table bound is not an unsigned upper limit at RVA {bound.ip:#x}")
        if (compare is None or compare.mnemonic != M.CMP or compare.op0_kind != OpKind.REGISTER
                or compare.op1_kind not in (OpKind.IMMEDIATE8TO32, OpKind.IMMEDIATE8TO64,
                                            OpKind.IMMEDIATE32, OpKind.IMMEDIATE32TO64)
                or RegisterExt.size(compare.op0_register) not in (4, 8)):
            raise ValueError(f"unbounded jump-table index at RVA {bound.ip:#x}")
        limit = compare.immediate(1) & ((1 << RegisterExt.size(compare.op0_register) * 8) - 1)
        count = limit + 1 if bound.condition_code == CC.A else limit
        if not 0 < count <= MAX_CASES:
            raise ValueError(f"unsupported jump-table size {count} at RVA {compare.ip:#x}")
        entries = struct.unpack(f"<{count}{'I' if kind == 'image' else 'i'}",
                                image_bytes(self.pe, table, 4 * count))
        self.claim(table, table + 4 * count, -table, "jump table")
        targets = [entry if kind == "image" else (table + entry) & 0xffffffff for entry in entries]
        self.dispatches[ip] = (index, targets, (load.ip, add.ip), list(reversed(between)),
                               target, (compare, bound, base, anchor))
        self.successors[ip] = targets
        return list(reversed(targets))

    def prove(self, ip, edges, constants):
        index, _, sequence, window, _, (compare, bound, base, anchor) = self.dispatches[ip]
        if any(address in edges for address in (bound.ip, *(i.ip for i in window), *sequence, ip)):
            raise ValueError(f"second entry into the dispatch block at RVA {ip:#x}")
        if constants.get(sequence[0], {}).get(base) != anchor:
            raise ValueError(f"table base {NAMES[base]} differs on another path to RVA {sequence[0]:#x}")
        # Extend the straight-line window back from the compare while it has a single entry.
        start = compare
        prefix = []
        while start.ip not in edges and start.ip != self.entry and len(prefix) < 16:
            before = self.ending.get(start.ip)
            if before is None or before.flow_control != FlowControl.NEXT:
                break
            prefix.append(before)
            start = before
        values = {}
        counter = iter(range(1 << 30))

        def value(reg):
            if reg not in values:
                values[reg] = (next(counter), "full")
            return values[reg]

        def step(ins):
            if ins.op_count == 0 or ins.op0_kind != OpKind.REGISTER:
                return
            dst = ins.op0_register
            width = RegisterExt.size(dst)
            if ins.mnemonic == M.CMP:
                return
            if ins.mnemonic == M.MOV and ins.op1_kind == OpKind.REGISTER and width in (4, 8):
                number, form = value(full(ins.op1_register))
                values[full(dst)] = (number, form) if width == 8 else (number, "zext32")
            elif (ins.mnemonic == M.MOVSXD and ins.op1_kind == OpKind.REGISTER
                  and RegisterExt.size(ins.op1_register) == 4):
                values[full(dst)] = (value(full(ins.op1_register))[0], "sext32")
            elif ins.mnemonic not in (M.CMP, M.TEST):
                values[full(dst)] = (next(counter), "zext32" if width == 4 else "full")

        for ins in reversed(prefix):
            step(ins)
        bounded, bounded_form = value(full(compare.op0_register))
        compared_width = RegisterExt.size(compare.op0_register)
        for ins in window:
            step(ins)
        number, form = value(index)
        if number != bounded or not (form in ("zext32", "sext32") or (compared_width == 8 and form == bounded_form)):
            raise ValueError(f"jump-table index {NAMES[index]} is not the bounded value at RVA {ip:#x}")


class Lifter:
    def __init__(self, offsets=None, frame_base=0):
        self.used = set()
        self.flags = False
        # Stack offset before each instruction, and the offset that frame[0] stands for.
        self.offsets = offsets or {}
        self.frame_base = frame_base
        self.frame = False

    def reg(self, register):
        width = RegisterExt.size(register)
        if register in HIGH_BYTES or width not in (1, 2, 4, 8):
            raise ValueError(f"unsupported register {register}")
        if full(register) == R.RSP:
            raise ValueError("frame address escapes through rsp")
        name = NAMES[full(register)]
        self.used.add(full(register))
        return name if width == 8 else f"static_cast<{unsigned(width)}>({name})"

    def slot(self, offset):
        """The host address of the frame byte at the given offset from entry RSP."""
        self.frame = True
        return f"(fp + {offset - self.frame_base:#x}ull)"

    def address(self, ins):
        if ins.memory_base == R.RSP and ins.memory_index == R.NONE and not ins.has_segment_prefix:
            if ins.mnemonic == M.LEA:
                raise ValueError(f"frame address escapes at RVA {ins.ip:#x}")
            return self.slot(self.offsets[ins.ip] + to_signed(ins.memory_displacement))
        if (ins.memory_base in (R.RIP, R.EIP, R.RSP, R.ESP) or ins.memory_index in (R.RSP, R.ESP)
                or ins.has_segment_prefix or ins.memory_base == R.NONE):
            raise ValueError(f"unsupported memory operand at RVA {ins.ip:#x}")
        parts = [self.reg(ins.memory_base)]
        if ins.memory_index != R.NONE:
            if not RegisterExt.is_gpr64(ins.memory_index):
                raise ValueError(f"unsupported memory index at RVA {ins.ip:#x}")
            parts.append(f"{self.reg(ins.memory_index)} * {ins.memory_index_scale}u")
        displacement = ins.memory_displacement & ((1 << 64) - 1)
        if displacement:
            parts.append(f"{displacement:#x}ull")
        if RegisterExt.is_gpr32(ins.memory_base):
            # A 67h prefix selects 32-bit addressing: the sum wraps at 32 bits and is zero-extended.
            return f"static_cast<std::uint64_t>(static_cast<std::uint32_t>({' + '.join(parts)}))"
        return "(" + " + ".join(parts) + ")"

    def operand(self, ins, number, width):
        kind = ins.op_kind(number)
        if kind == OpKind.REGISTER:
            return self.reg(ins.op_register(number))
        if kind == OpKind.MEMORY:
            return f"load<{unsigned(width)}>({self.address(ins)})"
        if kind in (OpKind.IMMEDIATE8, OpKind.IMMEDIATE8TO16, OpKind.IMMEDIATE8TO32, OpKind.IMMEDIATE8TO64,
                    OpKind.IMMEDIATE16, OpKind.IMMEDIATE32, OpKind.IMMEDIATE32TO64, OpKind.IMMEDIATE64):
            return f"static_cast<{unsigned(width)}>({ins.immediate(number) & ((1 << width * 8) - 1):#x}ull)"
        raise ValueError(f"unsupported operand at RVA {ins.ip:#x}")

    def write(self, ins, width, expression):
        if ins.op0_kind == OpKind.MEMORY:
            return f"store<{unsigned(width)}>({self.address(ins)}, static_cast<{unsigned(width)}>({expression}));"
        register = ins.op0_register
        if register in HIGH_BYTES:
            raise ValueError(f"unsupported register {register}")
        if width not in (1, 2, 4, 8) or RegisterExt.size(register) != width:
            raise ValueError(f"unsupported partial register write at RVA {ins.ip:#x}")
        self.used.add(full(register))
        name = NAMES[full(register)]
        if width < 4:
            # An 8- or 16-bit destination leaves the rest of the register unchanged.
            mask = (1 << width * 8) - 1
            return f"{name} = ({name} & ~{mask:#x}ull) | static_cast<{unsigned(width)}>({expression});"
        return f"{name} = static_cast<{unsigned(width)}>({expression});"

    def width(self, ins):
        if ins.op0_kind == OpKind.REGISTER:
            return RegisterExt.size(ins.op0_register)
        if ins.op0_kind == OpKind.MEMORY:
            return _memory_width(ins)
        return 0

    def condition(self, producer, jcc):
        code = CONDITIONS.get(jcc.condition_code)
        width = self.width(producer)
        u, s = unsigned(width), signed(width)
        if producer.mnemonic in (M.CMP, M.SUB):
            a, b = f"static_cast<{u}>(fa)", f"static_cast<{u}>(fb)"
            result = f"static_cast<{s}>(static_cast<{u}>({a} - {b}))"
            table = {"e": f"{a} == {b}", "ne": f"{a} != {b}", "b": f"{a} < {b}", "ae": f"{a} >= {b}",
                     "be": f"{a} <= {b}", "a": f"{a} > {b}", "l": f"static_cast<{s}>({a}) < static_cast<{s}>({b})",
                     "ge": f"static_cast<{s}>({a}) >= static_cast<{s}>({b})",
                     "le": f"static_cast<{s}>({a}) <= static_cast<{s}>({b})",
                     "g": f"static_cast<{s}>({a}) > static_cast<{s}>({b})",
                     "s": f"{result} < 0", "ns": f"{result} >= 0"}
        else:
            result = f"static_cast<{u}>(fa)"
            table = {"e": f"{result} == 0", "ne": f"{result} != 0",
                     "s": f"static_cast<{s}>({result}) < 0", "ns": f"static_cast<{s}>({result}) >= 0"}
            if producer.mnemonic in (M.TEST, M.AND, M.OR, M.XOR):
                # Logical operations clear CF and OF.
                table.update({"b": "false", "ae": "true", "be": f"{result} == 0", "a": f"{result} != 0",
                              "l": table["s"], "ge": table["ns"],
                              "le": f"{result} == 0 || {table['s']}", "g": f"{result} != 0 && {table['ns']}"})
        if code not in table:
            raise ValueError(f"unsupported condition after {producer.mnemonic} at RVA {jcc.ip:#x}")
        return table[code]

    def statement(self, ins, sets_flags=False, producer=None):
        """Return C++ for one instruction, the low bytes it reads from each full register and the
        width it writes to each full register.

        sets_flags records the flag inputs for a later consumer; producer is the
        instruction whose flags a SETcc or CMOVcc reads."""
        mnemonic = ins.mnemonic
        width = self.width(ins)
        reads, writes = {}, {}

        def read(register):
            reads[full(register)] = max(reads.get(full(register), 0), RegisterExt.size(register))

        def note(*numbers):
            for number in numbers:
                kind = ins.op_kind(number)
                if kind == OpKind.REGISTER:
                    read(ins.op_register(number))
                elif kind == OpKind.MEMORY:
                    for register in (ins.memory_base, ins.memory_index):
                        # An RSP base addresses the frame, whose bytes the definedness pass checks.
                        if register not in (R.NONE, R.RSP):
                            read(register)

        if ins.op_count and ins.op0_kind == OpKind.REGISTER:
            writes[full(ins.op0_register)] = width
        elif ins.op_count and ins.op0_kind == OpKind.MEMORY:
            note(0)
        if mnemonic == M.NOP:
            return "", {}, {}
        if mnemonic == M.MOV:
            note(1)
            return self.write(ins, width, self.operand(ins, 1, width)), reads, writes
        if mnemonic in (M.MOVZX, M.MOVSX, M.MOVSXD):
            note(1)
            source_width = (RegisterExt.size(ins.op1_register) if ins.op1_kind == OpKind.REGISTER
                            else _memory_width(ins))
            value = self.operand(ins, 1, source_width)
            if mnemonic != M.MOVZX:
                value = f"static_cast<{unsigned(width)}>(static_cast<{signed(width)}>(static_cast<{signed(source_width)}>({value})))"
            return self.write(ins, width, value), reads, writes
        if mnemonic == M.LEA:
            note(1)
            return self.write(ins, width, self.address(ins)), reads, writes
        if mnemonic in (*ALU, M.CMP, M.TEST):
            same = (mnemonic in (M.XOR, M.SUB) and ins.op0_kind == ins.op1_kind == OpKind.REGISTER
                    and ins.op0_register == ins.op1_register)
            if not same:
                note(0, 1)
            a = self.operand(ins, 0, width) if not same else f"static_cast<{unsigned(width)}>(0)"
            b = self.operand(ins, 1, width) if not same else a
            operator = ALU.get(mnemonic, "&")
            lines = []
            if sets_flags:
                self.flags = True
                if mnemonic in (M.CMP, M.SUB):
                    lines.append(f"fa = {a}; fb = {b};")
                else:
                    lines.append(f"fa = static_cast<{unsigned(width)}>({a} {operator} {b});")
            if mnemonic in ALU:
                lines.append(self.write(ins, width, f"{a} {operator} {b}"))
            else:
                writes.clear()
            return " ".join(lines), reads, writes
        if mnemonic in (M.INC, M.DEC, M.NEG, M.NOT):
            note(0)
            a = self.operand(ins, 0, width)
            value = {M.INC: f"{a} + 1u", M.DEC: f"{a} - 1u", M.NEG: f"0u - {a}", M.NOT: f"~{a}"}[mnemonic]
            line = self.write(ins, width, value)
            if sets_flags:
                if mnemonic not in (M.INC, M.DEC):
                    raise ValueError(f"unsupported flag producer at RVA {ins.ip:#x}")
                self.flags = True
                line = f"fa = static_cast<{unsigned(width)}>({value}); " + line
            return line, reads, writes
        if mnemonic in SHIFTS:
            note(0)
            a = self.operand(ins, 0, width)
            # x86 masks the count to six bits for 64-bit operands and to five bits otherwise, so an
            # 8- or 16-bit destination can be shifted by up to 31. The shift is done in 64 bits
            # (count < 64, no host overflow) and the write truncates the result to the destination.
            mask = 63 if width == 8 else 31
            if ins.op1_kind == OpKind.REGISTER:
                if ins.op1_register != R.CL:
                    raise ValueError(f"unsupported shift count at RVA {ins.ip:#x}")
                read(R.CL)
                count = f"({self.reg(R.RCX)} & {mask}u)"
            else:
                count = f"{ins.immediate(1) & mask}u"
            if mnemonic == M.SAR:
                a = f"static_cast<std::int64_t>(static_cast<{signed(width)}>({a}))"
            else:
                a = f"static_cast<std::uint64_t>({a})"
            return self.write(ins, width, f"{a} {SHIFTS[mnemonic]} {count}"), reads, writes
        if mnemonic == M.IMUL and ins.op_count in (2, 3):
            note(*range(ins.op_count))
            if ins.op_count == 2:
                a, b = self.operand(ins, 0, width), self.operand(ins, 1, width)
            else:
                a, b = self.operand(ins, 1, width), self.operand(ins, 2, width)
            if width == 2:
                # Two uint16_t factors would be promoted to int, where 0xffff * 0xffff overflows.
                a, b = f"static_cast<std::uint32_t>({a})", f"static_cast<std::uint32_t>({b})"
            return self.write(ins, width, f"{a} * {b}"), reads, writes
        if mnemonic in SETCC:
            condition = self.condition(producer, ins)
            if ins.op0_kind == OpKind.MEMORY:
                return f"store<std::uint8_t>({self.address(ins)}, static_cast<std::uint8_t>({condition}));", reads, writes
            # A byte write: it keeps bits 8..63 of the full register and does not read them.
            return self.write(ins, 1, condition), reads, writes
        if mnemonic in CMOVCC:
            if width not in (4, 8):
                raise ValueError(f"unsupported partial register write at RVA {ins.ip:#x}")
            note(0, 1)
            condition = self.condition(producer, ins)
            source, destination = self.operand(ins, 1, width), self.operand(ins, 0, width)
            # A 32-bit CMOVcc zero-extends the destination even when the condition is false,
            # and a memory source is read either way.
            if ins.op1_kind == OpKind.MEMORY:
                line = self.write(ins, width, f"({condition}) ? source : {destination}")
                # A volatile read: when the condition folds to false the compiler may not drop the load,
                # which faults on hardware for an inaccessible source.
                source = f"load_always<{unsigned(width)}>({self.address(ins)})"
                return f"{{ const {unsigned(width)} source = {source}; {line} }}", reads, writes
            return self.write(ins, width, f"({condition}) ? {source} : {destination}"), reads, writes
        if mnemonic == M.RET:
            # Which bytes of RAX are a return value is decided by the definedness pass in lift().
            return "return rax;", {}, {}
        raise ValueError(f"unsupported instruction at RVA {ins.ip:#x}: {ins}")


def _memory_width(ins):
    width = MemorySizeExt.size(ins.memory_size)
    if width not in (1, 2, 4, 8):
        raise ValueError(f"unsupported memory width at RVA {ins.ip:#x}")
    return width


def flag_producer(function, edges, consumer):
    """The instruction whose flags consumer reads, within one straight-line run."""
    current = consumer
    for _ in range(MAX_FLAG_DISTANCE):
        before = function.ending.get(current.ip)
        if (current.ip in edges or current.ip == function.entry or before is None
                or before.flow_control != FlowControl.NEXT):
            raise ValueError(f"flags cross a join before RVA {consumer.ip:#x}")
        if before.mnemonic not in FLAG_NEUTRAL:
            if before.mnemonic not in FLAG_PRODUCERS:
                raise ValueError(f"unsupported flag producer at RVA {before.ip:#x}")
            return before
        current = before
    raise ValueError(f"flag producer too far before RVA {consumer.ip:#x}")


def is_stack_adjust(ins):
    return (ins.mnemonic in (M.SUB, M.ADD) and ins.op_count == 2 and ins.op0_kind == OpKind.REGISTER
            and ins.op0_register == R.RSP)


def stack_offsets(function):
    """RSP before each reachable instruction, as a constant offset from its value at entry."""
    offsets = {function.entry: 0}
    work = [function.entry]
    while work:
        ip = work.pop()
        ins = function.instructions[ip]
        offset = after = offsets[ip]
        if ins.mnemonic in (M.PUSH, M.POP):
            if ins.op0_kind != OpKind.REGISTER or not RegisterExt.is_gpr64(ins.op0_register) \
                    or ins.op0_register == R.RSP:
                raise ValueError(f"unsupported stack operation at RVA {ip:#x}: {ins}")
            after += -8 if ins.mnemonic == M.PUSH else 8
        elif is_stack_adjust(ins):
            if ins.op1_kind not in (OpKind.IMMEDIATE8TO64, OpKind.IMMEDIATE32TO64):
                raise ValueError(f"stack pointer write at RVA {ip:#x}: {ins}")
            amount = to_signed(ins.immediate(1))
            after += -amount if ins.mnemonic == M.SUB else amount
        elif ins.mnemonic == M.RET:
            pass
        elif ins.is_stack_instruction or (ins.op_count and ins.op0_kind == OpKind.REGISTER
                                          and full(ins.op0_register) == R.RSP and ins.mnemonic not in (M.CMP, M.TEST)):
            raise ValueError(f"stack pointer write at RVA {ip:#x}: {ins}")
        if not -MAX_FRAME <= after <= 0 or after % 8:
            raise ValueError(f"unsupported stack offset {after} after RVA {ip:#x}")
        for successor in function.successors[ip]:
            if successor not in offsets:
                offsets[successor] = after
                work.append(successor)
            elif offsets[successor] != after:
                raise ValueError(f"stack offset differs at join RVA {successor:#x}: "
                                 f"{offsets[successor]} and {after}")
    # Checked once every path has been walked, so a mismatched join is reported as such
    # whichever path reaches a RET first.
    for ip in sorted(offsets):
        if function.instructions[ip].mnemonic == M.RET and offsets[ip] != 0:
            raise ValueError(f"stack offset {offsets[ip]} at RET at RVA {ip:#x}")
    return offsets


_INFO = InstructionInfoFactory()


def frame_accesses(ins, offset):
    """(kind, frame offset, width) for each RSP-based memory access of one instruction."""
    accesses = []
    for memory in _INFO.info(ins).used_memory():
        if R.RSP not in (memory.base, memory.index) and R.ESP not in (memory.base, memory.index):
            continue
        if memory.base != R.RSP or memory.index != R.NONE or ins.has_segment_prefix:
            raise ValueError(f"unsupported memory operand at RVA {ins.ip:#x}")
        if memory.access == OpAccess.NO_MEM_ACCESS:
            raise ValueError(f"frame address escapes at RVA {ins.ip:#x}")
        width = MemorySizeExt.size(memory.memory_size)
        at = offset + to_signed(memory.displacement)
        if at < offset:
            raise ValueError(f"frame access below the stack pointer at RVA {ins.ip:#x}")
        if at < HOME_BEGIN and at + width > 0:
            raise ValueError(f"return address slot accessed at RVA {ins.ip:#x}")
        if at + width > HOME_END:
            raise ValueError(f"stack argument accessed at RVA {ins.ip:#x}")
        if memory.access in (OpAccess.READ, OpAccess.COND_READ, OpAccess.READ_WRITE, OpAccess.READ_COND_WRITE):
            accesses.append(("read", at, width))
        if memory.access in (OpAccess.WRITE, OpAccess.READ_WRITE):
            accesses.append(("write", at, width))
        elif memory.access in (OpAccess.COND_WRITE, OpAccess.READ_COND_WRITE):
            raise ValueError(f"conditional frame write at RVA {ins.ip:#x}")
    return accesses


def lift(pe, name, arguments=DEFAULT_ARGUMENTS):
    """Translate one export that takes the given number of integer arguments, 3 or 4."""
    if not isinstance(arguments, int) or arguments not in ARGUMENT_COUNTS:
        raise ValueError(f"unsupported argument count {arguments!r} for export {name}: expected 3 or 4")
    entry = export_rva(pe, name)
    function = Function(pe, entry)
    function.arguments = ARGUMENT_REGISTERS[:arguments]
    edges = function.explore()
    offsets = stack_offsets(function)
    lowest = min(offsets.values())
    # Entry RSP is 8 mod 16 after the caller's CALL. frame[0] stands for an offset that is also
    # 8 mod 16, so the 16-byte-aligned array gives every slot its native alignment.
    frame_base = lowest - (lowest - HOME_BEGIN) % 16
    lifter = Lifter(offsets, frame_base)
    order = sorted(function.instructions)
    instructions = function.instructions
    producers = {ip: flag_producer(function, edges, ins) for ip, ins in instructions.items()
                 if ins.flow_control == FlowControl.CONDITIONAL_BRANCH or ins.mnemonic in SETCC | CMOVCC}
    for producer in producers.values():
        if is_stack_adjust(producer):
            raise ValueError(f"flags of a stack adjustment are read after RVA {producer.ip:#x}")
    sets_flags = {producer.ip for producer in producers.values()}
    loads = {sequence[0]: index for index, _, sequence, _, _, _ in function.dispatches.values()}
    skipped = {ip for _, _, sequence, _, _, _ in function.dispatches.values() for ip in sequence}
    body = {}
    effects = {}
    for ip in order:
        ins = instructions[ip]
        if ins.has_lock_prefix or ins.has_rep_prefix or ins.has_repne_prefix:
            raise ValueError(f"unsupported prefix at RVA {ip:#x}")
        body[ip] = ""
        if ins.flow_control == FlowControl.CONDITIONAL_BRANCH:
            body[ip] = f"if ({lifter.condition(producers[ip], ins)}) goto loc_{ins.near_branch_target:x};"
            effects[ip] = ({}, {}, set(), [])
        elif ins.flow_control == FlowControl.UNCONDITIONAL_BRANCH:
            body[ip] = f"goto loc_{ins.near_branch_target:x};"
            effects[ip] = ({}, {}, set(), [])
        elif ins.mnemonic in (M.PUSH, M.POP):
            register = full(ins.op0_register)
            name_ = lifter.reg(register)
            if ins.mnemonic == M.PUSH:
                at = offsets[ip] - 8
                body[ip] = f"store<std::uint64_t>({lifter.slot(at)}, {name_});"
                effects[ip] = ({register: 8}, {}, set(), [("push", at, register)])
            else:
                at = offsets[ip]
                body[ip] = f"{name_} = load<std::uint64_t>({lifter.slot(at)});"
                effects[ip] = ({}, {}, set(), [("pop", at, register)])
        elif is_stack_adjust(ins):
            # The offset pass accounts for the adjustment; the frame array needs no code.
            effects[ip] = ({}, {}, set(), [])
        elif ip in function.dispatches:
            index, targets, _, _, target, _ = function.dispatches[ip]
            cases = {}
            for number, destination in enumerate(targets):
                cases.setdefault(destination, []).append(number)
            lines = [f"switch ({lifter.reg(index)}) {{"]
            for destination, numbers in cases.items():
                labels = " ".join(f"case {n}:" for n in numbers)
                lines.append(f"    {labels} goto loc_{destination:x};")
            lines += ["    default: std::abort();", "}"]
            body[ip] = "\n    ".join(lines)
            # The guest jump target has no host meaning after the dispatch.
            effects[ip] = ({}, {}, {target}, [])
        elif ip in skipped:
            # The table load reads the 64-bit index; it and the add leave guest addresses behind.
            effects[ip] = ({loads[ip]: 8} if ip in loads else {}, {}, {full(ins.op0_register)}, [])
        elif ins.mnemonic == M.LEA and ins.is_ip_rel_memory_operand and RegisterExt.is_gpr64(ins.op0_register):
            # A guest address is only usable as a jump-table base, which the switch replaces.
            effects[ip] = ({}, {}, {full(ins.op0_register)}, [])
        else:
            # RET reads the return address, which the translation does not model.
            frame = [] if ins.mnemonic == M.RET else frame_accesses(ins, offsets[ip])
            text, reads, writes = lifter.statement(ins, ip in sets_flags, producers.get(ip))
            body[ip] = text
            effects[ip] = (reads, writes, set(), frame)

    # Per register: (low bytes defined on every path: 0, 1, 2 or 8; may hold a guest address that
    # the translation does not reproduce; still holds the caller's value). Per frame byte: written
    # on every path ("defined"), or holding the caller's value of a register saved by PUSH. Arguments
    # are defined at entry and the nonvolatile registers hold the caller's values; joins meet as
    # (min, or, and), and a frame byte survives a join only when both paths agree on it.
    def transfer(regs_in, slots_in, ip):
        reads, writes, kills, frame = effects[ip]
        regs, slots = dict(regs_in), dict(slots_in)
        for r, width in writes.items():
            # A 32- or 64-bit write defines all eight bytes; an 8- or 16-bit write keeps the rest.
            regs[r] = (8, False, False) if width >= 4 else (max(regs[r][0], width), regs[r][1], False)
        for r in kills:
            regs[r] = (0, True, False)
        for kind, at, extra in frame:
            if kind == "write":
                slots.update(dict.fromkeys(range(at, at + extra), "defined"))
            elif kind == "push":
                tag = ("saved", extra) if regs_in[extra][2] else "defined"
                slots.update(dict.fromkeys(range(at, at + 8), tag))
            elif kind == "pop":
                tags = {slots_in.get(b) for b in range(at, at + 8)}
                regs[extra] = ((0, False, True) if tags == {("saved", extra)} else
                               (8, False, False) if tags == {"defined"} else (0, False, False))
        return regs, slots

    state = dict.fromkeys(order)
    state[entry] = ({r: (8 if r in function.arguments else 0, False, r in NONVOLATILE) for r in NAMES}, {})
    changed = True
    while changed:
        changed = False
        for ip in order:
            if state[ip] is None:
                continue
            regs, slots = transfer(*state[ip], ip)
            for successor in function.successors[ip]:
                old = state[successor]
                if old is None:
                    merged = (regs, slots)
                else:
                    merged = ({r: (min(old[0][r][0], regs[r][0]), old[0][r][1] or regs[r][1],
                                   old[0][r][2] and regs[r][2]) for r in NAMES},
                              {b: tag for b, tag in old[1].items() if slots.get(b) == tag})
                if merged != old:
                    state[successor] = merged
                    changed = True
    widths = []
    for ip in order:
        regs, slots = state[ip]
        reads, _, _, frame = effects[ip]
        saving = {extra for kind, _, extra in frame if kind == "push" and regs[extra][2]}
        missing = [r for r, width in reads.items() if r not in saving and regs[r][0] < width]
        if missing:
            r = min(missing)
            if regs[r][2]:
                raise ValueError(f"nonvolatile register {NAMES[r]} holds the caller's value at RVA {ip:#x}")
            raise ValueError(f"undefined register {NAMES[r]} at RVA {ip:#x}")
        for kind, at, extra in frame:
            if kind in ("write", "push") and any(regs[r][1] for r in reads):
                raise ValueError(f"guest address stored in the frame at RVA {ip:#x}")
            if kind == "read":
                tags = [slots.get(b) for b in range(at, at + extra)]
                if any(isinstance(tag, tuple) for tag in tags):
                    raise ValueError(f"saved register slot read at RVA {ip:#x}")
                if None in tags:
                    raise ValueError(f"undefined frame bytes at offset {at} at RVA {ip:#x}")
            elif kind == "pop":
                tags = {slots.get(b) for b in range(at, at + 8)}
                if tags != {"defined"} and tags != {("saved", extra)}:
                    what = "undefined frame bytes" if None in tags else "another register's save slot"
                    raise ValueError(f"pop of {NAMES[extra]} reads {what} at RVA {ip:#x}")
        if instructions[ip].mnemonic == M.RET:
            for r in sorted(NONVOLATILE, key=lambda r: NAMES[r]):
                if not regs[r][2]:
                    raise ValueError(f"nonvolatile register {NAMES[r]} is not restored at RET at RVA {ip:#x}")
            defined, guest, _ = regs[R.RAX]
            if guest:
                raise ValueError(f"return value may hold a guest address at RVA {ip:#x}")
            widths.append(defined)
    # Consecutive low RAX bytes defined at every reachable RET; None when no RET is reachable.
    function.return_width = min(widths) if widths else None

    labels = set(edges)
    if order[0] != entry:
        # Emission follows address order. Code that the function reaches below its entry
        # comes first in the text, so execution has to start with a jump to the entry.
        labels.add(entry)
    for position, ip in enumerate(order):
        ins = instructions[ip]
        following = order[position + 1] if position + 1 < len(order) else None
        if ins.flow_control in (FlowControl.NEXT, FlowControl.CONDITIONAL_BRANCH) and following != ins.next_ip:
            labels.add(ins.next_ip)
    locals_ = sorted((lifter.used | {R.RAX}) - set(function.arguments), key=lambda r: NAMES[r])
    declarations = [f"word {NAMES[r]} = 0;" for r in locals_]
    if lifter.flags:
        declarations.append("word fa = 0, fb = 0;")
    if lifter.frame:
        # One frame per invocation: the deepest offset reached up to the end of the home area.
        declarations.append(f"alignas(16) unsigned char frame[{HOME_END - frame_base:#x}] = {{}};")
        declarations.append("const word fp = reinterpret_cast<word>(frame);")
    text = [f"    goto loc_{entry:x};"] if order[0] != entry else []
    for position, ip in enumerate(order):
        if ip in labels:
            text.append(f"loc_{ip:x}:")
        if body[ip]:
            text.append("    " + body[ip])
        ins = instructions[ip]
        following = order[position + 1] if position + 1 < len(order) else None
        if ins.flow_control in (FlowControl.NEXT, FlowControl.CONDITIONAL_BRANCH) and following != ins.next_ip:
            text.append(f"    goto loc_{ins.next_ip:x};")
    signature = (f'extern "C" __declspec(dllexport) word lifted_{name}'
                 f'({", ".join(f"word {NAMES[r]}" for r in function.arguments)})')
    comment = []
    if function.return_width is None:
        comment = ["// RAX: no reachable normal return"]
    elif function.return_width < 8:
        comment = [f"// RAX: {function.return_width} low byte(s) are defined at every reachable RET"]
    source = [*comment, signature + " {", *("    " + d for d in declarations), *text, "}"]
    return "\n".join(source), function


PRELUDE = """#include <cstdint>
#include <cstdlib>
#include <cstring>
using word = std::uint64_t;
template <class T> static T load(word address) {
    T value;
    std::memcpy(&value, reinterpret_cast<const void*>(address), sizeof(value));
    return value;
}
// A read the compiler must perform even when its value is unused (a CMOVcc memory source).
template <class T> static T load_always(word address) {
    return *reinterpret_cast<const volatile T*>(address);
}
template <class T> static void store(word address, T value) {
    std::memcpy(reinterpret_cast<void*>(address), &value, sizeof(value));
}
"""


def parse_entry(spec):
    """Split an export-list item, "name" or "name:N", into the name and its integer argument count."""
    name, colon, count = spec.partition(":")
    if not colon:
        return name, DEFAULT_ARGUMENTS
    if not (count.isascii() and count.isdigit()):
        raise ValueError(f"unsupported argument count {count!r} for export {name}: expected 3 or 4")
    return name, int(count)


def translate_pe(pe, names):
    if pe.FILE_HEADER.Machine != 0x8664 or pe.OPTIONAL_HEADER.Magic != 0x20B:
        raise ValueError("expected AMD64 PE32+")
    functions = [lift(pe, *parse_entry(name)) for name in names]
    return PRELUDE + "\n".join(f"\n{source}" for source, _ in functions) + "\n", [f for _, f in functions]


def translate(image, names):
    with pefile.PE(str(image), fast_load=True) as pe:
        return translate_pe(pe, names)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", type=Path)
    parser.add_argument("exports", help='comma-separated export names; "name:4" translates that export with a '
                                        "fourth integer argument in R9, and the host must pass four")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        source, functions = translate(args.image, args.exports.split(","))
        args.output.write_text(source, encoding="utf-8")
        for function in functions:
            print(f"RVA {function.entry:#x}: {len(function.instructions)} instructions, "
                  f"{sum(len(d[1]) for d in function.dispatches.values())} table entries, "
                  + ("no reachable normal return" if function.return_width is None else
                     f"{function.return_width} defined RAX byte(s) at every reachable RET"))
    except (ValueError, OSError, pefile.PEFormatError) as error:
        parser.exit(1, f"{error}\n")
