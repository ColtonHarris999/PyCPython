"""Port of Python/assemble.c: turn the optimized instruction sequence into
bytecode, location table, exception table and finally a code object."""

import sys

from . import opcodes as op
from .opcodes import *  # noqa: F401,F403
from .errors import NO_LOCATION, NEXT_LOCATION
from .codegen import (const_cache_merge_one, consts_dict_keys_inorder, CO_VARARGS,
                      CO_VARKEYWORDS)
from .flowgraph import (cfg_from_instruction_sequence, optimize_code_unit,
                        optimized_cfg_to_instruction_sequence)
from .marshal_writer import CodeSpec, build_code

PY_CODE_LOCATION_INFO_SHORT0 = 0
PY_CODE_LOCATION_INFO_ONE_LINE0 = 10
PY_CODE_LOCATION_INFO_ONE_LINE1 = 11
PY_CODE_LOCATION_INFO_ONE_LINE2 = 12
PY_CODE_LOCATION_INFO_NO_COLUMNS = 13
PY_CODE_LOCATION_INFO_LONG = 14
PY_CODE_LOCATION_INFO_NONE = 15

CO_FAST_ARG_POS = 0x02
CO_FAST_ARG_KW = 0x04
CO_FAST_ARG_VAR = 0x08
CO_FAST_HIDDEN = 0x10
CO_FAST_LOCAL = 0x20
CO_FAST_CELL = 0x40
CO_FAST_FREE = 0x80

END_SEND_OFFSET = 5
CONTINUATION_BIT = 64


def instr_size(instr):
    opcode = instr.opcode
    oparg = instr.oparg
    assert not op.is_pseudo_instr(opcode), (opcode, op.opname.get(opcode))
    assert op.has_arg(opcode) or oparg == 0
    extended_args = (0xFFFFFF < oparg) + (0xFFFF < oparg) + (0xFF < oparg)
    caches = op.caches(opcode)
    return extended_args + 1 + caches


class Assembler:
    def __init__(self, firstlineno):
        self.bytecode = bytearray()
        self.offset = 0
        self.except_table = bytearray()
        self.lineno = firstlineno
        self.linetable = bytearray()

    # -- exception table ----------------------------------------------------

    def write_except_byte(self, byte):
        self.except_table.append(byte)

    def emit_exception_table_item(self, value, msb):
        assert (msb | 128) == 128
        assert 0 <= value < (1 << 30)
        if value >= 1 << 24:
            self.write_except_byte((value >> 24) | CONTINUATION_BIT | msb)
            msb = 0
        if value >= 1 << 18:
            self.write_except_byte(((value >> 18) & 0x3f) | CONTINUATION_BIT | msb)
            msb = 0
        if value >= 1 << 12:
            self.write_except_byte(((value >> 12) & 0x3f) | CONTINUATION_BIT | msb)
            msb = 0
        if value >= 1 << 6:
            self.write_except_byte(((value >> 6) & 0x3f) | CONTINUATION_BIT | msb)
            msb = 0
        self.write_except_byte((value & 0x3f) | msb)

    def emit_exception_table_entry(self, start, end, handler_offset, h_startdepth,
                                   h_preserve_lasti):
        size = end - start
        assert end > start
        target = handler_offset
        depth = h_startdepth - 1
        if h_preserve_lasti > 0:
            depth -= 1
        assert depth >= 0
        depth_lasti = (depth << 1) | h_preserve_lasti
        self.emit_exception_table_item(start, 1 << 7)
        self.emit_exception_table_item(size, 0)
        self.emit_exception_table_item(target, 0)
        self.emit_exception_table_item(depth_lasti, 0)

    def assemble_exception_table(self, instrs):
        ioffset = 0
        h_label = -1
        h_startdepth = -1
        h_preserve_lasti = -1
        start = -1
        for instr in instrs.instrs:
            if instr.except_handler_label != h_label:
                if h_label >= 0:
                    handler_offset = instrs.instrs[h_label].offset
                    self.emit_exception_table_entry(start, ioffset, handler_offset,
                                                    h_startdepth, h_preserve_lasti)
                start = ioffset
                h_label = instr.except_handler_label
                h_startdepth = instr.except_handler_startdepth
                h_preserve_lasti = instr.except_handler_preserve_lasti
            ioffset += instr_size(instr)
        if h_label >= 0:
            handler_offset = instrs.instrs[h_label].offset
            self.emit_exception_table_entry(start, ioffset, handler_offset,
                                            h_startdepth, h_preserve_lasti)

    # -- location table --------------------------------------------------------

    def write_location_byte(self, val):
        self.linetable.append(val & 255)

    def write_location_first_byte(self, code, length):
        assert (code & 15) == code
        self.linetable.append(128 | (code << 3) | (length - 1))

    def write_location_varint(self, val):
        while val >= 64:
            self.linetable.append(64 | (val & 63))
            val >>= 6
        self.linetable.append(val)

    def write_location_signed_varint(self, val):
        if val < 0:
            uval = ((-val) << 1) | 1
        else:
            uval = val << 1
        self.write_location_varint(uval)

    def write_location_info_short_form(self, length, column, end_column):
        assert 0 < length <= 8
        column_low_bits = column & 7
        column_group = column >> 3
        assert column < 80
        assert end_column >= column
        assert end_column - column < 16
        self.write_location_first_byte(PY_CODE_LOCATION_INFO_SHORT0 + column_group, length)
        self.write_location_byte((column_low_bits << 4) | (end_column - column))

    def write_location_info_oneline_form(self, length, line_delta, column, end_column):
        assert 0 < length <= 8
        assert 0 <= line_delta < 3
        assert column < 128
        assert end_column < 128
        self.write_location_first_byte(PY_CODE_LOCATION_INFO_ONE_LINE0 + line_delta, length)
        self.write_location_byte(column)
        self.write_location_byte(end_column)

    def write_location_info_long_form(self, loc, length):
        assert 0 < length <= 8
        self.write_location_first_byte(PY_CODE_LOCATION_INFO_LONG, length)
        self.write_location_signed_varint(loc.lineno - self.lineno)
        assert loc.end_lineno >= loc.lineno
        self.write_location_varint(loc.end_lineno - loc.lineno)
        self.write_location_varint(loc.col_offset + 1)
        self.write_location_varint(loc.end_col_offset + 1)

    def write_location_info_none(self, length):
        self.write_location_first_byte(PY_CODE_LOCATION_INFO_NONE, length)

    def write_location_info_no_column(self, length, line_delta):
        self.write_location_first_byte(PY_CODE_LOCATION_INFO_NO_COLUMNS, length)
        self.write_location_signed_varint(line_delta)

    def write_location_info_entry(self, loc, isize):
        if loc.lineno == NO_LOCATION.lineno:
            self.write_location_info_none(isize)
            return
        line_delta = loc.lineno - self.lineno
        column = loc.col_offset
        end_column = loc.end_col_offset
        if column < 0 or end_column < 0:
            if loc.end_lineno == loc.lineno or loc.end_lineno < 0:
                self.write_location_info_no_column(isize, line_delta)
                self.lineno = loc.lineno
                return
        elif loc.end_lineno == loc.lineno:
            if (line_delta == 0 and column < 80 and end_column - column < 16
                    and end_column >= column):
                self.write_location_info_short_form(isize, column, end_column)
                return
            if 0 <= line_delta < 3 and column < 128 and end_column < 128:
                self.write_location_info_oneline_form(isize, line_delta, column, end_column)
                self.lineno = loc.lineno
                return
        self.write_location_info_long_form(loc, isize)
        self.lineno = loc.lineno

    def emit_location(self, loc, isize):
        if isize == 0:
            return
        while isize > 8:
            self.write_location_info_entry(loc, 8)
            isize -= 8
        self.write_location_info_entry(loc, isize)

    def assemble_location_info(self, instrs, firstlineno):
        self.lineno = firstlineno
        loc = NO_LOCATION
        seq = instrs.instrs
        for i in range(len(seq) - 1, -1, -1):
            instr = seq[i]
            if instr.loc == NEXT_LOCATION:
                if op.is_terminator_opcode(instr.opcode):
                    instr.loc = NO_LOCATION
                else:
                    assert i < len(seq) - 1
                    instr.loc = seq[i + 1].loc
        size = 0
        for instr in seq:
            if not (loc == instr.loc):
                self.emit_location(loc, size)
                loc = instr.loc
                size = 0
            size += instr_size(instr)
        self.emit_location(loc, size)

    # -- bytecode ------------------------------------------------------------------

    def emit_instr(self, instr):
        opcode = instr.opcode
        assert not op.is_pseudo_instr(opcode)
        oparg = instr.oparg
        assert op.has_arg(opcode) or oparg == 0
        caches = op.caches(opcode)
        size = instr_size(instr)
        n = size - caches
        code = self.bytecode
        if n >= 4:
            code.append(EXTENDED_ARG)
            code.append((oparg >> 24) & 0xFF)
        if n >= 3:
            code.append(EXTENDED_ARG)
            code.append((oparg >> 16) & 0xFF)
        if n >= 2:
            code.append(EXTENDED_ARG)
            code.append((oparg >> 8) & 0xFF)
        code.append(opcode)
        code.append(oparg & 0xFF)
        for _ in range(caches):
            code.append(CACHE)
            code.append(0)
        self.offset += size

    def assemble_emit(self, instrs, first_lineno, const_cache):
        for instr in instrs.instrs:
            self.emit_instr(instr)
        self.assemble_location_info(instrs, self.lineno)
        self.assemble_exception_table(instrs)
        self.except_table_bytes = const_cache_merge_one(const_cache, bytes(self.except_table))
        self.linetable_bytes = const_cache_merge_one(const_cache, bytes(self.linetable))
        self.bytecode_bytes = const_cache_merge_one(const_cache, bytes(self.bytecode))


def dict_keys_inorder(d, offset):
    size = len(d)
    result = [None] * size
    for k, v in d.items():
        i = v - offset
        assert 0 <= i < size
        result[i] = k
    return tuple(result)


def compute_localsplus_info(umd, nlocalsplus, flags):
    """Returns (names, kinds) lists of length nlocalsplus."""
    names = [None] * nlocalsplus
    kinds = [0] * nlocalsplus
    argvarkinds = [
        (umd.u_posonlyargcount, CO_FAST_ARG_POS),
        (umd.u_argcount, CO_FAST_ARG_POS | CO_FAST_ARG_KW),
        (umd.u_kwonlyargcount, CO_FAST_ARG_KW),
        (1 if flags & CO_VARARGS else 0, CO_FAST_ARG_VAR | CO_FAST_ARG_POS),
        (1 if flags & CO_VARKEYWORDS else 0, CO_FAST_ARG_VAR | CO_FAST_ARG_KW),
        (-1, 0),
    ]
    items = list(umd.u_varnames.items())
    pos = 0
    max_ = 0
    for count, akind in argvarkinds:
        max_ = sys.maxsize if count < 0 else max_ + count
        while pos < max_ and pos < len(items):
            k, offset = items[pos]
            pos += 1
            assert 0 <= offset < nlocalsplus
            kind = CO_FAST_LOCAL | akind
            if k in umd.u_fasthidden:
                kind |= CO_FAST_HIDDEN
            if k in umd.u_cellvars:
                kind |= CO_FAST_CELL
            names[offset] = k
            kinds[offset] = kind
    nlocals = len(umd.u_varnames)
    numdropped = 0
    cellvar_offset = -1
    for k, v in umd.u_cellvars.items():
        if k in umd.u_varnames:
            numdropped += 1
            continue
        cellvar_offset = v + nlocals - numdropped
        assert cellvar_offset < nlocalsplus
        names[cellvar_offset] = k
        kinds[cellvar_offset] = CO_FAST_CELL
    for k, v in umd.u_freevars.items():
        offset = v + nlocals - numdropped
        assert offset < nlocalsplus
        assert offset > cellvar_offset
        names[offset] = k
        kinds[offset] = CO_FAST_FREE
    return names, kinds


def makecode(umd, a, const_cache, constslist, maxdepth, nlocalsplus, code_flags, filename):
    names = dict_keys_inorder(umd.u_names, 0)
    names = const_cache_merge_one(const_cache, names)
    consts = tuple(constslist)
    consts = const_cache_merge_one(const_cache, consts)
    posonlyargcount = umd.u_posonlyargcount
    posorkwargcount = umd.u_argcount
    kwonlyargcount = umd.u_kwonlyargcount
    lp_names, lp_kinds = compute_localsplus_info(umd, nlocalsplus, code_flags)
    localsplusnames = tuple(lp_names)
    localsplusnames = const_cache_merge_one(const_cache, localsplusnames)
    name = umd.u_name
    if umd.u_qualname is None or umd.u_qualname is umd.u_name:
        qualname = name
    else:
        qualname = umd.u_qualname
    spec = CodeSpec(
        argcount=posonlyargcount + posorkwargcount,
        posonlyargcount=posonlyargcount,
        kwonlyargcount=kwonlyargcount,
        stacksize=maxdepth,
        flags=code_flags,
        code=a.bytecode_bytes,
        consts=consts,
        names=names,
        localsplusnames=localsplusnames,
        localspluskinds=bytes(lp_kinds),
        filename=filename,
        name=name,
        qualname=qualname,
        firstlineno=umd.u_firstlineno,
        linetable=a.linetable_bytes,
        exceptiontable=a.except_table_bytes,
        name_interned=not name.startswith("<generic parameters of "),
    )
    return build_code(spec)


def resolve_jump_offsets(instrs):
    seq = instrs.instrs
    for instr in seq:
        if op.has_jump(instr.opcode):
            instr.target = instr.oparg
    while True:
        totsize = 0
        for instr in seq:
            instr.offset = totsize
            totsize += instr_size(instr)
        extended_arg_recompile = False
        offset = 0
        for instr in seq:
            isize = instr_size(instr)
            offset += isize
            if op.has_jump(instr.opcode):
                target = seq[instr.target]
                instr.oparg = target.offset
                if instr.opcode == END_ASYNC_FOR:
                    instr.oparg = offset - instr.oparg - END_SEND_OFFSET
                elif instr.oparg < offset:
                    assert op.is_backwards_jump_opcode(instr.opcode)
                    instr.oparg = offset - instr.oparg
                else:
                    assert not op.is_backwards_jump_opcode(instr.opcode)
                    instr.oparg = instr.oparg - offset
                if instr_size(instr) != isize:
                    extended_arg_recompile = True
        if not extended_arg_recompile:
            break


def resolve_unconditional_jumps(instrs):
    for i, instr in enumerate(instrs.instrs):
        is_forward = instr.oparg > i
        if instr.opcode == JUMP:
            instr.opcode = JUMP_FORWARD if is_forward else JUMP_BACKWARD
        elif instr.opcode == JUMP_NO_INTERRUPT:
            instr.opcode = JUMP_FORWARD if is_forward else JUMP_BACKWARD_NO_INTERRUPT
        else:
            if op.has_jump(instr.opcode) and op.is_pseudo_instr(instr.opcode):
                raise SystemError("unreachable pseudo jump")


def make_code_object(umd, const_cache, consts, maxdepth, instrs, nlocalsplus, code_flags,
                     filename):
    """_PyAssemble_MakeCodeObject."""
    instrs.apply_label_map()
    resolve_unconditional_jumps(instrs)
    resolve_jump_offsets(instrs)
    a = Assembler(umd.u_firstlineno)
    a.assemble_emit(instrs, umd.u_firstlineno, const_cache)
    return makecode(umd, a, const_cache, consts, maxdepth, nlocalsplus, code_flags, filename)


def optimize_and_assemble_code_unit(u, const_cache, code_flags, filename):
    """optimize_and_assemble_code_unit (compile.c)."""
    consts = consts_dict_keys_inorder(u.u_metadata.u_consts)
    g = cfg_from_instruction_sequence(u.u_instr_sequence)
    nlocals = len(u.u_metadata.u_varnames)
    nparams = len(u.u_ste.varnames)
    assert u.u_metadata.u_firstlineno
    optimize_code_unit(g, consts, const_cache, nlocals, nparams, u.u_metadata.u_firstlineno)
    stackdepth, nlocalsplus, optimized_instrs = optimized_cfg_to_instruction_sequence(
        g, u.u_metadata, code_flags)
    return make_code_object(u.u_metadata, const_cache, consts, stackdepth, optimized_instrs,
                            nlocalsplus, code_flags, filename)
