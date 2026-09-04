"""Port of Python/flowgraph.c: control flow graph construction and the
bytecode optimizations that run on it."""

from . import opcodes as op
from .opcodes import *  # noqa: F401,F403
from .errors import SourceLocation, NO_LOCATION
from .instrseq import InstructionSequence, NO_LABEL
from .codegen import (const_cache_merge_one, CO_MAXBLOCKS, _PY_STACK_USE_GUIDELINE,
                      CO_GENERATOR, CO_COROUTINE, CO_ASYNC_GENERATOR)

RESUME_OPARG_DEPTH1_MASK = 4

MAX_COPY_SIZE = 4
MIN_CONST_SEQUENCE_SIZE = 3
MAX_INT_SIZE = 128
MAX_COLLECTION_SIZE = 256
MAX_STR_SIZE = 4096
MAX_TOTAL_ITEMS = 1024
INT_MIN = -(1 << 31)
INT_MAX = (1 << 31) - 1


def LOCATION(lno, end_lno, col, end_col):
    return SourceLocation(lno, end_lno, col, end_col)


class CfgInstr:
    __slots__ = ('opcode', 'oparg', 'loc', 'target', 'except_')

    def __init__(self, opcode, oparg, loc, target=None, except_=None):
        self.opcode = opcode
        self.oparg = oparg
        self.loc = loc
        self.target = target
        self.except_ = except_

    def copy(self):
        return CfgInstr(self.opcode, self.oparg, self.loc, self.target, self.except_)

    def set_op0(self, opcode):
        assert not op.has_arg(opcode)
        self.opcode = opcode
        self.oparg = 0

    def set_op1(self, opcode, oparg):
        assert op.has_arg(opcode)
        self.opcode = opcode
        self.oparg = oparg

    def __repr__(self):
        return "<%s %r %r>" % (op.opname.get(self.opcode, self.opcode), self.oparg, self.loc)


class BasicBlock:
    def __init__(self):
        self.label = NO_LABEL
        self.exceptstack = None
        self.instr = []
        self.next = None
        self.unsafe_locals_mask = 0
        self.predecessors = 0
        self.startdepth = 0
        self.preserve_lasti = 0
        self.visited = 0
        self.except_handler = 0
        self.cold = 0
        self.warm = 0
        # C keeps the instruction array allocated when the block shrinks;
        # a later basicblock_addop() reuses the slot without resetting its
        # i_except field.  ``stale`` maps physical slot index -> i_except
        # of the instruction that used to occupy it, so we can reproduce
        # the exception table the C compiler produces.
        self.stale = {}

    @property
    def iused(self):
        return len(self.instr)

    def set_instrs(self, new):
        old = self.instr
        for k in range(len(new), len(old)):
            self.stale[k] = old[k]
        self.instr = new

    def first_instr_even_if_stale(self):
        """C reads b_instr[0] of an empty block whose array is still
        allocated; return the stale instruction in that case (None if the
        block never had an instruction)."""
        if self.instr:
            return self.instr[0]
        return self.stale.get(0)

    def grew(self):
        n = len(self.instr)
        for k in [k for k in self.stale if k < n]:
            del self.stale[k]

    def last_instr(self):
        if self.instr:
            return self.instr[-1]
        return None

    def __repr__(self):
        return "<block label=%d n=%d>" % (self.label, len(self.instr))


def is_block_push(i):
    return op.is_block_push_opcode(i.opcode)


def is_jump(i):
    return op.has_jump(i.opcode)


def basicblock_addop(b, opcode, oparg, loc):
    assert op.is_valid_opcode(opcode)
    assert not op.is_assembler_opcode(opcode)
    assert op.has_arg(opcode) or op.has_target(opcode) or oparg == 0
    assert 0 <= oparg < (1 << 30)
    i = len(b.instr)
    old = b.stale.pop(i, None)
    b.instr.append(CfgInstr(opcode, oparg, loc, None, old.except_ if old is not None else None))


def basicblock_add_jump(b, opcode, target, loc):
    last = b.last_instr()
    if last is not None and is_jump(last):
        raise SystemError("basicblock_add_jump: block already ends with a jump")
    basicblock_addop(b, opcode, target.label, loc)
    last = b.last_instr()
    last.target = target


def basicblock_append_instructions(to, frm):
    for i in frm.instr:
        to.instr.append(i.copy())
    to.grew()


def basicblock_nofallthrough(b):
    last = b.last_instr()
    return last is not None and (op.is_scope_exit_opcode(last.opcode)
                                 or op.is_unconditional_jump_opcode(last.opcode))


def BB_HAS_FALLTHROUGH(b):
    return not basicblock_nofallthrough(b)


def basicblock_exits_scope(b):
    last = b.last_instr()
    return last is not None and op.is_scope_exit_opcode(last.opcode)


def basicblock_has_eval_break(b):
    for i in b.instr:
        if op.flags(i.opcode) & HAS_EVAL_BREAK_FLAG:
            return True
    return False


def iter_blocks(entry):
    b = entry
    while b is not None:
        yield b
        b = b.next


class CfgBuilder:
    def __init__(self):
        self.block_list = []
        block = self.new_block()
        self.curblock = self.entryblock = block
        self.current_label = NO_LABEL

    def new_block(self):
        b = BasicBlock()
        self.block_list.append(b)
        return b

    def copy_basicblock(self, block):
        assert basicblock_nofallthrough(block)
        result = self.new_block()
        basicblock_append_instructions(result, block)
        return result

    def use_next_block(self, block):
        self.curblock.next = block
        self.curblock = block
        return block

    def current_block_is_terminated(self):
        last = self.curblock.last_instr()
        if last is not None and op.is_terminator_opcode(last.opcode):
            return True
        if self.current_label != NO_LABEL:
            if last is not None or self.curblock.label != NO_LABEL:
                return True
            else:
                self.curblock.label = self.current_label
                self.current_label = NO_LABEL
        return False

    def maybe_start_new_block(self):
        if self.current_block_is_terminated():
            b = self.new_block()
            b.label = self.current_label
            self.current_label = NO_LABEL
            self.use_next_block(b)

    def use_label(self, lbl):
        self.current_label = lbl
        self.maybe_start_new_block()

    def addop(self, opcode, oparg, loc):
        self.maybe_start_new_block()
        basicblock_addop(self.curblock, opcode, oparg, loc)


def next_nonempty_block(b):
    while b is not None and b.iused == 0:
        b = b.next
    return b


# ---- CFG preprocessing --------------------------------------------------------

def normalize_jumps_in_block(g, b):
    last = b.last_instr()
    if last is None or not op.is_conditional_jump_opcode(last.opcode):
        return
    assert not op.is_assembler_opcode(last.opcode)
    is_forward = last.target.visited == 0
    if is_forward:
        basicblock_addop(b, NOT_TAKEN, 0, last.loc)
        return
    reversed_opcode = {
        POP_JUMP_IF_NOT_NONE: POP_JUMP_IF_NONE,
        POP_JUMP_IF_NONE: POP_JUMP_IF_NOT_NONE,
        POP_JUMP_IF_FALSE: POP_JUMP_IF_TRUE,
        POP_JUMP_IF_TRUE: POP_JUMP_IF_FALSE,
    }[last.opcode]
    target = last.target
    backwards_jump = g.new_block()
    basicblock_addop(backwards_jump, NOT_TAKEN, 0, last.loc)
    basicblock_add_jump(backwards_jump, JUMP, target, last.loc)
    backwards_jump.startdepth = target.startdepth
    last.opcode = reversed_opcode
    last.target = b.next
    backwards_jump.cold = b.cold
    backwards_jump.next = b.next
    b.next = backwards_jump


def normalize_jumps(g):
    for b in iter_blocks(g.entryblock):
        b.visited = 0
    for b in iter_blocks(g.entryblock):
        b.visited = 1
        normalize_jumps_in_block(g, b)


def check_cfg(g):
    for b in iter_blocks(g.entryblock):
        n = b.iused
        for i, instr in enumerate(b.instr):
            opcode = instr.opcode
            assert not op.is_assembler_opcode(opcode)
            if op.is_terminator_opcode(opcode):
                if i != n - 1:
                    raise SystemError("malformed control flow graph.")


def get_max_label(entryblock):
    lbl = -1
    for b in iter_blocks(entryblock):
        if b.label > lbl:
            lbl = b.label
    return lbl


def translate_jump_labels_to_targets(entryblock):
    label2block = {}
    for b in iter_blocks(entryblock):
        if b.label >= 0:
            label2block[b.label] = b
    for b in iter_blocks(entryblock):
        for instr in b.instr:
            assert instr.target is None
            if op.has_target(instr.opcode):
                lbl = instr.oparg
                instr.target = label2block[lbl]
                assert instr.target.label == lbl


def mark_except_handlers(entryblock):
    for b in iter_blocks(entryblock):
        for instr in b.instr:
            if is_block_push(instr):
                instr.target.except_handler = 1


class ExceptStack:
    def __init__(self):
        self.handlers = [None]
        self.depth = 0

    def copy(self):
        c = ExceptStack()
        c.handlers = list(self.handlers)
        c.depth = self.depth
        return c


def push_except_block(stack, setup):
    assert is_block_push(setup)
    opcode = setup.opcode
    target = setup.target
    if opcode == SETUP_WITH or opcode == SETUP_CLEANUP:
        target.preserve_lasti = 1
    assert stack.depth <= CO_MAXBLOCKS
    stack.depth += 1
    if len(stack.handlers) <= stack.depth:
        stack.handlers.append(target)
    else:
        stack.handlers[stack.depth] = target
    return target


def pop_except_block(stack):
    assert stack.depth > 0
    stack.depth -= 1
    return stack.handlers[stack.depth]


def except_stack_top(stack):
    return stack.handlers[stack.depth]


def make_cfg_traversal_stack(entryblock):
    for b in iter_blocks(entryblock):
        b.visited = 0
    return []


def get_stack_effects(opcode, oparg, jump):
    if opcode < 0:
        return None
    popped = op.num_popped(opcode, oparg)
    pushed = op.num_pushed(opcode, oparg)
    if popped < 0 or pushed < 0:
        return None
    if op.is_block_push_opcode(opcode) and not jump:
        return 0
    return pushed - popped


def stack_effect(opcode, oparg):
    """PyCompile_OpcodeStackEffect"""
    return get_stack_effects(opcode, oparg, -1)


def stackdepth_push(sp, b, depth):
    if not (b.startdepth < 0 or b.startdepth == depth):
        raise ValueError("Invalid CFG, inconsistent stackdepth")
    if b.startdepth < depth and b.startdepth < 100:
        assert b.startdepth < 0
        b.startdepth = depth
        sp.append(b)


def calculate_stackdepth(g):
    entryblock = g.entryblock
    for b in iter_blocks(entryblock):
        b.startdepth = INT_MIN
    sp = make_cfg_traversal_stack(entryblock)
    maxdepth = 0
    stackdepth_push(sp, entryblock, 0)
    while sp:
        b = sp.pop()
        depth = b.startdepth
        assert depth >= 0
        nxt = b.next
        for instr in b.instr:
            eff = get_stack_effects(instr.opcode, instr.oparg, 0)
            if eff is None:
                raise SystemError("Invalid stack effect for opcode=%d, arg=%i"
                                  % (instr.opcode, instr.oparg))
            new_depth = depth + eff
            if new_depth < 0:
                raise ValueError("Invalid CFG, stack underflow")
            maxdepth = max(maxdepth, depth)
            if op.has_target(instr.opcode) and instr.opcode != END_ASYNC_FOR:
                eff = get_stack_effects(instr.opcode, instr.oparg, 1)
                if eff is None:
                    raise SystemError("Invalid stack effect for opcode=%d, arg=%i"
                                      % (instr.opcode, instr.oparg))
                target_depth = depth + eff
                assert target_depth >= 0
                maxdepth = max(maxdepth, depth)
                stackdepth_push(sp, instr.target, target_depth)
            depth = new_depth
            assert not op.is_assembler_opcode(instr.opcode)
            if (op.is_unconditional_jump_opcode(instr.opcode)
                    or op.is_scope_exit_opcode(instr.opcode)):
                nxt = None
                break
        if nxt is not None:
            assert BB_HAS_FALLTHROUGH(b)
            stackdepth_push(sp, nxt, depth)
    return maxdepth


def label_exception_targets(entryblock):
    todo = make_cfg_traversal_stack(entryblock)
    except_stack = ExceptStack()
    todo.append(entryblock)
    entryblock.visited = 1
    entryblock.exceptstack = except_stack
    while todo:
        b = todo.pop()
        assert b.visited == 1
        except_stack = b.exceptstack
        assert except_stack is not None
        b.exceptstack = None
        handler = except_stack_top(except_stack)
        last_yield_except_depth = -1
        for instr in b.instr:
            if is_block_push(instr):
                if not instr.target.visited:
                    instr.target.exceptstack = except_stack.copy()
                    todo.append(instr.target)
                    instr.target.visited = 1
                handler = push_except_block(except_stack, instr)
            elif instr.opcode == POP_BLOCK:
                handler = pop_except_block(except_stack)
                instr.set_op0(NOP)
            elif is_jump(instr):
                instr.except_ = handler
                assert instr is b.instr[-1]
                if not instr.target.visited:
                    if BB_HAS_FALLTHROUGH(b):
                        instr.target.exceptstack = except_stack.copy()
                    else:
                        instr.target.exceptstack = except_stack
                        except_stack = None
                    todo.append(instr.target)
                    instr.target.visited = 1
            elif instr.opcode == YIELD_VALUE:
                instr.except_ = handler
                last_yield_except_depth = except_stack.depth
            elif instr.opcode == RESUME:
                instr.except_ = handler
                if instr.oparg != RESUME_AT_FUNC_START:
                    assert last_yield_except_depth >= 0
                    if last_yield_except_depth == 1:
                        instr.oparg |= RESUME_OPARG_DEPTH1_MASK
                    last_yield_except_depth = -1
            else:
                instr.except_ = handler
        if BB_HAS_FALLTHROUGH(b) and not b.next.visited:
            assert except_stack is not None
            b.next.exceptstack = except_stack
            todo.append(b.next)
            b.next.visited = 1


# ---- CFG optimizations ----------------------------------------------------------

def remove_unreachable(entryblock):
    for b in iter_blocks(entryblock):
        b.predecessors = 0
    sp = make_cfg_traversal_stack(entryblock)
    entryblock.predecessors = 1
    sp.append(entryblock)
    entryblock.visited = 1
    while sp:
        b = sp.pop()
        if b.next is not None and BB_HAS_FALLTHROUGH(b):
            if not b.next.visited:
                assert b.next.predecessors == 0
                sp.append(b.next)
                b.next.visited = 1
            b.next.predecessors += 1
        for instr in b.instr:
            if is_jump(instr) or is_block_push(instr):
                target = instr.target
                if not target.visited:
                    sp.append(target)
                    target.visited = 1
                target.predecessors += 1
    for b in iter_blocks(entryblock):
        if b.predecessors == 0:
            b.set_instrs([])
            b.except_handler = 0


def basicblock_remove_redundant_nops(bb):
    instrs = bb.instr
    n = len(instrs)
    out = []
    prev_lineno = -1
    for src in range(n):
        ins = instrs[src]
        lineno = ins.loc.lineno
        if ins.opcode == NOP:
            if lineno < 0:
                continue
            if prev_lineno == lineno:
                continue
            if src < n - 1:
                next_lineno = instrs[src + 1].loc.lineno
                if next_lineno == lineno:
                    continue
                if next_lineno < 0:
                    instrs[src + 1].loc = ins.loc
                    continue
            else:
                nxt = next_nonempty_block(bb.next)
                if nxt is not None:
                    next_loc = NO_LOCATION
                    for ni in nxt.instr:
                        if ni.opcode == NOP and ni.loc.lineno < 0:
                            continue
                        next_loc = ni.loc
                        break
                    if lineno == next_loc.lineno:
                        continue
        out.append(ins)
        prev_lineno = lineno
    num_removed = n - len(out)
    bb.set_instrs(out)
    return num_removed


def remove_redundant_nops(g):
    changes = 0
    for b in iter_blocks(g.entryblock):
        changes += basicblock_remove_redundant_nops(b)
    return changes


def remove_redundant_nops_and_pairs(entryblock):
    done = False
    while not done:
        done = True
        prev_instr = None
        instr = None
        for b in iter_blocks(entryblock):
            basicblock_remove_redundant_nops(b)
            if b.label != NO_LABEL:
                instr = None
            for i in range(b.iused):
                prev_instr = instr
                instr = b.instr[i]
                prev_opcode = prev_instr.opcode if prev_instr is not None else 0
                prev_oparg = prev_instr.oparg if prev_instr is not None else 0
                opcode = instr.opcode
                is_redundant_pair = False
                if opcode == POP_TOP:
                    if prev_opcode == LOAD_CONST or prev_opcode == LOAD_SMALL_INT:
                        is_redundant_pair = True
                    elif prev_opcode == COPY and prev_oparg == 1:
                        is_redundant_pair = True
                if is_redundant_pair:
                    prev_instr.set_op0(NOP)
                    instr.set_op0(NOP)
                    done = False
            if (instr is not None and is_jump(instr)) or not BB_HAS_FALLTHROUGH(b):
                instr = None


def remove_redundant_jumps(g):
    changes = 0
    for b in iter_blocks(g.entryblock):
        last = b.last_instr()
        if last is None:
            continue
        assert not op.is_assembler_opcode(last.opcode)
        if op.is_unconditional_jump_opcode(last.opcode):
            jump_target = next_nonempty_block(last.target)
            if jump_target is None:
                raise SystemError("jump with NULL target")
            nxt = next_nonempty_block(b.next)
            if jump_target is nxt:
                changes += 1
                last.set_op0(NOP)
    return changes


def basicblock_has_no_lineno(b):
    for i in b.instr:
        if i.loc.lineno >= 0:
            return False
    return True


def basicblock_inline_small_or_no_lineno_blocks(bb):
    last = bb.last_instr()
    if last is None:
        return 0
    if not op.is_unconditional_jump_opcode(last.opcode):
        return 0
    target = last.target
    small_exit_block = basicblock_exits_scope(target) and target.iused <= MAX_COPY_SIZE
    no_lineno_no_fallthrough = basicblock_has_no_lineno(target) and not BB_HAS_FALLTHROUGH(target)
    if small_exit_block or no_lineno_no_fallthrough:
        assert is_jump(last)
        removed_jump_opcode = last.opcode
        last.set_op0(NOP)
        basicblock_append_instructions(bb, target)
        if no_lineno_no_fallthrough:
            last = bb.last_instr()
            if op.is_unconditional_jump_opcode(last.opcode) and removed_jump_opcode == JUMP:
                last.opcode = JUMP
        target.predecessors -= 1
        return 1
    return 0


def inline_small_or_no_lineno_blocks(entryblock):
    while True:
        changes = False
        for b in iter_blocks(entryblock):
            if basicblock_inline_small_or_no_lineno_blocks(b):
                changes = True
        if not changes:
            break


def jump_thread(bb, inst, target, opcode):
    assert is_jump(inst)
    assert is_jump(target)
    assert inst is bb.last_instr()
    if inst.target is not target.target:
        inst.set_op0(NOP)
        basicblock_add_jump(bb, opcode, target.target, target.loc)
        return 1
    return 0


def loads_const(opcode):
    return op.has_const(opcode) or opcode == LOAD_SMALL_INT


def get_const_value(opcode, oparg, co_consts):
    assert loads_const(opcode)
    if opcode == LOAD_CONST:
        n = len(co_consts)
        if oparg < 0 or oparg >= n:
            raise ValueError("LOAD_CONST index %d is out of range for consts (len=%d)"
                             % (oparg, n))
        return co_consts[oparg]
    if opcode == LOAD_SMALL_INT:
        return oparg
    raise SystemError("Internal error: failed to get value of a constant")


def add_const(newconst, consts, const_cache, consts_index):
    newconst = const_cache_merge_one(const_cache, newconst)
    idx = consts_index.get(id(newconst))
    if idx is not None:
        return idx
    index = len(consts)
    consts.append(newconst)
    consts_index[id(newconst)] = index
    return index


def get_const_loading_instrs(bb, start, size):
    """Returns a list of `size` const-loading instructions ending at index
    `start` (skipping NOPs), or None."""
    assert start < bb.iused
    assert 0 <= size <= _PY_STACK_USE_GUIDELINE
    result = []
    while start >= 0 and size > 0:
        instr = bb.instr[start]
        start -= 1
        if instr.opcode == NOP:
            continue
        if not loads_const(instr.opcode):
            return None
        result.append(instr)
        size -= 1
    if size != 0:
        return None
    result.reverse()
    return result


def nop_out(instrs):
    for instr in instrs:
        assert instr.opcode != NOP
        instr.set_op0(NOP)
        instr.loc = NO_LOCATION


def maybe_instr_make_load_smallint(instr, newconst):
    if type(newconst) is int:
        val = newconst
        if -5 <= val < 257 and 0 <= val <= 255:
            instr.set_op1(LOAD_SMALL_INT, val)
            return 1
    return 0


def instr_make_load_const(instr, newconst, consts, const_cache, consts_index):
    if maybe_instr_make_load_smallint(instr, newconst):
        return
    oparg = add_const(newconst, consts, const_cache, consts_index)
    instr.set_op1(LOAD_CONST, oparg)


def fold_tuple_of_constants(bb, i, consts, const_cache, consts_index):
    instr = bb.instr[i]
    assert instr.opcode == BUILD_TUPLE
    seq_size = instr.oparg
    if seq_size > _PY_STACK_USE_GUIDELINE:
        return
    const_instrs = get_const_loading_instrs(bb, i - 1, seq_size)
    if const_instrs is None:
        return
    items = []
    for inst in const_instrs:
        items.append(get_const_value(inst.opcode, inst.oparg, consts))
    const_tuple = tuple(items)
    nop_out(const_instrs)
    instr_make_load_const(instr, const_tuple, consts, const_cache, consts_index)


def fold_constant_intrinsic_list_to_tuple(bb, i, consts, const_cache, consts_index):
    intrinsic = bb.instr[i]
    assert intrinsic.opcode == CALL_INTRINSIC_1
    assert intrinsic.oparg == INTRINSIC_LIST_TO_TUPLE
    consts_found = 0
    expect_append = True
    pos = i - 1
    while pos >= 0:
        instr = bb.instr[pos]
        opcode = instr.opcode
        oparg = instr.oparg
        if opcode == NOP:
            pos -= 1
            continue
        if opcode == BUILD_LIST and oparg == 0:
            if not expect_append:
                return
            items = [None] * consts_found
            for newpos in range(i - 1, pos - 1, -1):
                instr = bb.instr[newpos]
                if instr.opcode == NOP:
                    continue
                if loads_const(instr.opcode):
                    constant = get_const_value(instr.opcode, instr.oparg, consts)
                    assert consts_found > 0
                    consts_found -= 1
                    items[consts_found] = constant
                nop_out([instr])
            assert consts_found == 0
            instr_make_load_const(intrinsic, tuple(items), consts, const_cache, consts_index)
            return
        if expect_append:
            if opcode != LIST_APPEND or oparg != 1:
                return
        else:
            if not loads_const(opcode):
                return
            consts_found += 1
        expect_append = not expect_append
        pos -= 1


def optimize_lists_and_sets(bb, i, nextop, consts, const_cache, consts_index):
    instr = bb.instr[i]
    assert instr.opcode in (BUILD_LIST, BUILD_SET)
    contains_or_iter = nextop == GET_ITER or nextop == CONTAINS_OP
    seq_size = instr.oparg
    if (seq_size > _PY_STACK_USE_GUIDELINE
            or (seq_size < MIN_CONST_SEQUENCE_SIZE and not contains_or_iter)):
        return
    const_instrs = get_const_loading_instrs(bb, i - 1, seq_size)
    if const_instrs is None:
        if contains_or_iter and instr.opcode == BUILD_LIST:
            instr.set_op1(BUILD_TUPLE, instr.oparg)
        return
    items = []
    for inst in const_instrs:
        items.append(get_const_value(inst.opcode, inst.oparg, consts))
    const_result = tuple(items)
    if instr.opcode == BUILD_SET:
        const_result = frozenset(const_result)
    index = add_const(const_result, consts, const_cache, consts_index)
    nop_out(const_instrs)
    if contains_or_iter:
        instr.set_op1(LOAD_CONST, index)
    else:
        assert i >= 2
        bb.instr[i - 2].loc = instr.loc
        bb.instr[i - 2].set_op1(instr.opcode, 0)
        bb.instr[i - 1].set_op1(LOAD_CONST, index)
        bb.instr[i].set_op1(LIST_EXTEND if instr.opcode == BUILD_LIST else SET_UPDATE, 1)


def const_folding_check_complexity(obj, limit):
    if isinstance(obj, tuple):
        limit -= len(obj)
        for item in obj:
            if limit < 0:
                break
            limit = const_folding_check_complexity(item, limit)
            if limit < 0:
                return limit
    return limit


def _numbits(v):
    return abs(int(v)).bit_length()


def _as_size_t(w):
    """PyLong_AsSize_t: None means overflow/negative."""
    w = int(w)
    if w < 0 or w >= (1 << 64):
        return None
    return w


def _as_long(v):
    v = int(v)
    if v < -(1 << 63) or v >= (1 << 63):
        return -1  # overflow -> error in C; callers treat < 0 as bail
    return v


def const_folding_safe_multiply(v, w):
    if isinstance(v, int) and isinstance(w, int) and v != 0 and w != 0:
        vbits = _numbits(v)
        wbits = _numbits(w)
        if vbits + wbits > MAX_INT_SIZE:
            return None
    elif isinstance(v, int) and isinstance(w, tuple):
        size = len(w)
        if size:
            n = _as_long(v)
            if n < 0 or n > MAX_COLLECTION_SIZE // size:
                return None
            if n and const_folding_check_complexity(w, MAX_TOTAL_ITEMS // n) < 0:
                return None
    elif isinstance(v, int) and isinstance(w, (str, bytes)):
        size = len(w)
        if size:
            n = _as_long(v)
            if n < 0 or n > MAX_STR_SIZE // size:
                return None
    elif isinstance(w, int) and isinstance(v, (tuple, str, bytes)):
        return const_folding_safe_multiply(w, v)
    return v * w


def const_folding_safe_power(v, w):
    if isinstance(v, int) and isinstance(w, int) and v != 0 and w > 0:
        vbits = _numbits(v)
        wbits = _as_size_t(w)
        if wbits is None:
            return None
        if vbits > MAX_INT_SIZE // wbits:
            return None
    return pow(v, w)


def const_folding_safe_lshift(v, w):
    if isinstance(v, int) and isinstance(w, int) and v != 0 and w != 0:
        vbits = _numbits(v)
        wbits = _as_size_t(w)
        if wbits is None:
            return None
        if wbits > MAX_INT_SIZE or vbits > MAX_INT_SIZE - wbits:
            return None
    return v << w


def const_folding_safe_mod(v, w):
    if isinstance(v, (str, bytes)):
        return None
    return v % w


class _NoFold(Exception):
    pass


def eval_const_binop(left, oparg, right):
    if oparg == NB_ADD:
        return left + right
    if oparg == NB_SUBTRACT:
        return left - right
    if oparg == NB_MULTIPLY:
        return const_folding_safe_multiply(left, right)
    if oparg == NB_TRUE_DIVIDE:
        return left / right
    if oparg == NB_FLOOR_DIVIDE:
        return left // right
    if oparg == NB_REMAINDER:
        return const_folding_safe_mod(left, right)
    if oparg == NB_POWER:
        return const_folding_safe_power(left, right)
    if oparg == NB_LSHIFT:
        return const_folding_safe_lshift(left, right)
    if oparg == NB_RSHIFT:
        return left >> right
    if oparg == NB_OR:
        return left | right
    if oparg == NB_XOR:
        return left ^ right
    if oparg == NB_AND:
        return left & right
    if oparg == NB_SUBSCR:
        return left[right]
    if oparg == NB_MATRIX_MULTIPLY:
        return None
    raise SystemError("unreachable")


def fold_const_binop(bb, i, consts, const_cache, consts_index):
    binop = bb.instr[i]
    assert binop.opcode == BINARY_OP
    operands = get_const_loading_instrs(bb, i - 1, 2)
    if operands is None:
        return
    lhs_instr, rhs_instr = operands
    lhs = get_const_value(lhs_instr.opcode, lhs_instr.oparg, consts)
    rhs = get_const_value(rhs_instr.opcode, rhs_instr.oparg, consts)
    try:
        newconst = eval_const_binop(lhs, binop.oparg, rhs)
    except KeyboardInterrupt:
        raise
    except Exception:
        return
    if newconst is None:
        return
    nop_out(operands)
    instr_make_load_const(binop, newconst, consts, const_cache, consts_index)


def eval_const_unaryop(operand, opcode, oparg):
    if opcode == UNARY_NEGATIVE:
        return -operand
    if opcode == UNARY_INVERT:
        if type(operand) is bool:
            return None
        return ~operand
    if opcode == UNARY_NOT:
        return not operand
    if opcode == CALL_INTRINSIC_1:
        assert oparg == INTRINSIC_UNARY_POSITIVE
        return +operand
    raise SystemError("unreachable")


def fold_const_unaryop(bb, i, consts, const_cache, consts_index):
    unaryop = bb.instr[i]
    operands = get_const_loading_instrs(bb, i - 1, 1)
    if operands is None:
        return
    operand_instr = operands[0]
    operand = get_const_value(operand_instr.opcode, operand_instr.oparg, consts)
    try:
        newconst = eval_const_unaryop(operand, unaryop.opcode, unaryop.oparg)
    except KeyboardInterrupt:
        raise
    except Exception:
        return
    if newconst is None:
        return
    if unaryop.opcode == UNARY_NOT:
        assert type(newconst) is bool
    nop_out(operands)
    instr_make_load_const(unaryop, newconst, consts, const_cache, consts_index)


VISITED = -1


def swaptimize(block, ix):
    """Returns the new value of ix."""
    instructions = block.instr
    base = ix
    assert instructions[base].opcode == SWAP
    depth = instructions[base].oparg
    length = 0
    more = False
    limit = block.iused - base
    while True:
        length += 1
        if not (length < limit):
            break
        opcode = instructions[base + length].opcode
        if opcode == SWAP:
            depth = max(depth, instructions[base + length].oparg)
            more = True
        elif opcode != NOP:
            break
    if not more:
        return ix
    stack = list(range(depth))
    for i in range(length):
        if instructions[base + i].opcode == SWAP:
            oparg = instructions[base + i].oparg
            top = stack[0]
            stack[0] = stack[oparg - 1]
            stack[oparg - 1] = top
    current = length - 1
    for i in range(depth):
        if stack[i] == VISITED or stack[i] == i:
            continue
        j = i
        while True:
            if j:
                assert 0 <= current
                instructions[base + current].opcode = SWAP
                instructions[base + current].oparg = j + 1
                current -= 1
            if stack[j] == VISITED:
                assert j == i
                break
            next_j = stack[j]
            stack[j] = VISITED
            j = next_j
    while 0 <= current:
        instructions[base + current].set_op0(NOP)
        current -= 1
    return ix + length - 1


def SWAPPABLE(opcode):
    return opcode in (STORE_FAST, STORE_FAST_MAYBE_NULL, POP_TOP)


def STORES_TO(instr):
    if instr.opcode in (STORE_FAST, STORE_FAST_MAYBE_NULL):
        return instr.oparg
    return -1


def next_swappable_instruction(block, i, lineno):
    while True:
        i += 1
        if not (i < block.iused):
            break
        instruction = block.instr[i]
        if 0 <= lineno and instruction.loc.lineno != lineno:
            return -1
        if instruction.opcode == NOP:
            continue
        if SWAPPABLE(instruction.opcode):
            return i
        return -1
    return -1


def apply_static_swaps(block, i):
    while 0 <= i:
        assert i < block.iused
        swap = block.instr[i]
        if swap.opcode != SWAP:
            if swap.opcode == NOP or SWAPPABLE(swap.opcode):
                i -= 1
                continue
            return
        j = next_swappable_instruction(block, i, -1)
        if j < 0:
            return
        k = j
        lineno = block.instr[j].loc.lineno
        count = swap.oparg - 1
        while 0 < count:
            k = next_swappable_instruction(block, k, lineno)
            if k < 0:
                return
            count -= 1
        store_j = STORES_TO(block.instr[j])
        store_k = STORES_TO(block.instr[k])
        if store_j >= 0 or store_k >= 0:
            if store_j == store_k:
                return
            for idx in range(j + 1, k):
                store_idx = STORES_TO(block.instr[idx])
                if store_idx >= 0 and (store_idx == store_j or store_idx == store_k):
                    return
        swap.set_op0(NOP)
        block.instr[j], block.instr[k] = block.instr[k], block.instr[j]
        i -= 1


def basicblock_optimize_load_const(const_cache, bb, consts, consts_index):
    opcode = 0
    oparg = 0
    i = 0
    while i < bb.iused:
        inst = bb.instr[i]
        if inst.opcode == LOAD_CONST:
            constant = get_const_value(inst.opcode, inst.oparg, consts)
            maybe_instr_make_load_smallint(inst, constant)
        is_copy_of_load_const = (opcode == LOAD_CONST and inst.opcode == COPY and inst.oparg == 1)
        if not is_copy_of_load_const:
            opcode = inst.opcode
            oparg = inst.oparg
        assert not op.is_assembler_opcode(opcode)
        if opcode != LOAD_CONST and opcode != LOAD_SMALL_INT:
            i += 1
            continue
        nextop = bb.instr[i + 1].opcode if i + 1 < bb.iused else 0
        if nextop in (POP_JUMP_IF_FALSE, POP_JUMP_IF_TRUE, JUMP_IF_FALSE, JUMP_IF_TRUE):
            cnt = get_const_value(opcode, oparg, consts)
            is_true = bool(cnt)
            if stack_effect(nextop, 0) == -1:
                inst.set_op0(NOP)
            jump_if_true = nextop in (POP_JUMP_IF_TRUE, JUMP_IF_TRUE)
            if is_true == jump_if_true:
                bb.instr[i + 1].opcode = JUMP
            else:
                bb.instr[i + 1].set_op0(NOP)
        elif nextop == IS_OP:
            cnt = get_const_value(opcode, oparg, consts)
            if cnt is not None:
                i += 1
                continue
            if bb.iused <= i + 2:
                i += 1
                continue
            is_instr = bb.instr[i + 1]
            jump_instr = bb.instr[i + 2]
            if jump_instr.opcode == TO_BOOL:
                jump_instr.set_op0(NOP)
                if bb.iused <= i + 3:
                    i += 1
                    continue
                jump_instr = bb.instr[i + 3]
            invert = bool(is_instr.oparg)
            if jump_instr.opcode == POP_JUMP_IF_FALSE:
                invert = not invert
            elif jump_instr.opcode != POP_JUMP_IF_TRUE:
                i += 1
                continue
            inst.set_op0(NOP)
            is_instr.set_op0(NOP)
            jump_instr.opcode = POP_JUMP_IF_NOT_NONE if invert else POP_JUMP_IF_NONE
        elif nextop == TO_BOOL:
            cnt = get_const_value(opcode, oparg, consts)
            is_true = bool(cnt)
            index = add_const(is_true, consts, const_cache, consts_index)
            inst.set_op0(NOP)
            bb.instr[i + 1].set_op1(LOAD_CONST, index)
        i += 1


def optimize_load_const(const_cache, g, consts, consts_index):
    for b in iter_blocks(g.entryblock):
        basicblock_optimize_load_const(const_cache, b, consts, consts_index)


_NOP_INSTR = CfgInstr(NOP, 0, NO_LOCATION)


def optimize_basic_block(const_cache, bb, consts, consts_index):
    i = 0
    while i < bb.iused:
        inst = bb.instr[i]
        opcode = inst.opcode
        oparg = inst.oparg
        if op.has_target(opcode):
            assert inst.target.iused > 0
            target = inst.target.instr[0]
            assert not op.is_assembler_opcode(target.opcode)
        else:
            target = _NOP_INSTR
        nextop = bb.instr[i + 1].opcode if i + 1 < bb.iused else 0
        assert not op.is_assembler_opcode(opcode)
        if opcode == BUILD_TUPLE:
            if nextop == UNPACK_SEQUENCE and oparg == bb.instr[i + 1].oparg:
                if oparg == 1:
                    inst.set_op0(NOP)
                    bb.instr[i + 1].set_op0(NOP)
                    i += 1
                    continue
                if oparg in (2, 3):
                    inst.set_op0(NOP)
                    bb.instr[i + 1].opcode = SWAP
                    i += 1
                    continue
            fold_tuple_of_constants(bb, i, consts, const_cache, consts_index)
        elif opcode in (BUILD_LIST, BUILD_SET):
            optimize_lists_and_sets(bb, i, nextop, consts, const_cache, consts_index)
        elif opcode in (POP_JUMP_IF_NOT_NONE, POP_JUMP_IF_NONE):
            if target.opcode == JUMP:
                i -= jump_thread(bb, inst, target, inst.opcode)
        elif opcode == POP_JUMP_IF_FALSE:
            if target.opcode == JUMP:
                i -= jump_thread(bb, inst, target, POP_JUMP_IF_FALSE)
        elif opcode == POP_JUMP_IF_TRUE:
            if target.opcode == JUMP:
                i -= jump_thread(bb, inst, target, POP_JUMP_IF_TRUE)
        elif opcode == JUMP_IF_FALSE:
            if target.opcode in (JUMP, JUMP_IF_FALSE):
                i -= jump_thread(bb, inst, target, JUMP_IF_FALSE)
                i += 1
                continue
            if target.opcode == JUMP_IF_TRUE:
                assert inst.target is not inst.target.next
                inst.target = inst.target.next
                continue
        elif opcode == JUMP_IF_TRUE:
            if target.opcode in (JUMP, JUMP_IF_TRUE):
                i -= jump_thread(bb, inst, target, JUMP_IF_TRUE)
                i += 1
                continue
            if target.opcode == JUMP_IF_FALSE:
                assert inst.target is not inst.target.next
                inst.target = inst.target.next
                continue
        elif opcode in (JUMP, JUMP_NO_INTERRUPT):
            if target.opcode == JUMP:
                i -= jump_thread(bb, inst, target, JUMP)
                i += 1
                continue
            if target.opcode == JUMP_NO_INTERRUPT:
                i -= jump_thread(bb, inst, target, opcode)
                i += 1
                continue
        elif opcode == FOR_ITER:
            pass
        elif opcode == STORE_FAST:
            if (opcode == nextop and oparg == bb.instr[i + 1].oparg
                    and bb.instr[i].loc.lineno == bb.instr[i + 1].loc.lineno):
                bb.instr[i].opcode = POP_TOP
                bb.instr[i].oparg = 0
        elif opcode == SWAP:
            if oparg == 1:
                inst.set_op0(NOP)
        elif opcode == LOAD_GLOBAL:
            if nextop == PUSH_NULL and (oparg & 1) == 0:
                inst.set_op1(LOAD_GLOBAL, oparg | 1)
                bb.instr[i + 1].set_op0(NOP)
        elif opcode == COMPARE_OP:
            if nextop == TO_BOOL:
                inst.set_op0(NOP)
                bb.instr[i + 1].set_op1(COMPARE_OP, oparg | 16)
                i += 1
                continue
        elif opcode in (CONTAINS_OP, IS_OP):
            if nextop == TO_BOOL:
                inst.set_op0(NOP)
                bb.instr[i + 1].set_op1(opcode, oparg)
                i += 1
                continue
            if nextop == UNARY_NOT:
                inst.set_op0(NOP)
                inverted = oparg ^ 1
                assert inverted in (0, 1)
                bb.instr[i + 1].set_op1(opcode, inverted)
                i += 1
                continue
        elif opcode == TO_BOOL:
            if nextop == TO_BOOL:
                inst.set_op0(NOP)
                i += 1
                continue
        elif opcode in (UNARY_NOT, UNARY_INVERT, UNARY_NEGATIVE):
            if opcode == UNARY_NOT:
                if nextop == TO_BOOL:
                    inst.set_op0(NOP)
                    bb.instr[i + 1].set_op0(UNARY_NOT)
                    i += 1
                    continue
                if nextop == UNARY_NOT:
                    inst.set_op0(NOP)
                    bb.instr[i + 1].set_op0(NOP)
                    i += 1
                    continue
            fold_const_unaryop(bb, i, consts, const_cache, consts_index)
        elif opcode == CALL_INTRINSIC_1:
            if oparg == INTRINSIC_LIST_TO_TUPLE:
                if nextop == GET_ITER:
                    inst.set_op0(NOP)
                else:
                    fold_constant_intrinsic_list_to_tuple(bb, i, consts, const_cache,
                                                          consts_index)
            elif oparg == INTRINSIC_UNARY_POSITIVE:
                fold_const_unaryop(bb, i, consts, const_cache, consts_index)
        elif opcode == BINARY_OP:
            fold_const_binop(bb, i, consts, const_cache, consts_index)
        i += 1
    i = 0
    while i < bb.iused:
        inst = bb.instr[i]
        if inst.opcode == SWAP:
            i = swaptimize(bb, i)
            apply_static_swaps(bb, i)
        i += 1


def remove_redundant_nops_and_jumps(g):
    while True:
        removed_nops = remove_redundant_nops(g)
        removed_jumps = remove_redundant_jumps(g)
        if removed_nops + removed_jumps <= 0:
            break


def optimize_cfg(g, consts, const_cache, consts_index, firstlineno):
    check_cfg(g)
    inline_small_or_no_lineno_blocks(g.entryblock)
    remove_unreachable(g.entryblock)
    resolve_line_numbers(g, firstlineno)
    optimize_load_const(const_cache, g, consts, consts_index)
    for b in iter_blocks(g.entryblock):
        optimize_basic_block(const_cache, b, consts, consts_index)
    remove_redundant_nops_and_pairs(g.entryblock)
    remove_unreachable(g.entryblock)
    remove_redundant_nops_and_jumps(g)


def make_super_instruction(inst1, inst2, super_op):
    line1 = inst1.loc.lineno
    line2 = inst2.loc.lineno
    if line1 >= 0 and line2 >= 0 and line1 != line2:
        return
    if inst1.oparg >= 16 or inst2.oparg >= 16:
        return
    inst1.set_op1(super_op, (inst1.oparg << 4) | inst2.oparg)
    inst2.set_op0(NOP)


def insert_superinstructions(g):
    for b in iter_blocks(g.entryblock):
        for i in range(b.iused):
            inst = b.instr[i]
            nextop = b.instr[i + 1].opcode if i + 1 < b.iused else 0
            if inst.opcode == LOAD_FAST:
                if nextop == LOAD_FAST:
                    make_super_instruction(inst, b.instr[i + 1], LOAD_FAST_LOAD_FAST)
            elif inst.opcode == STORE_FAST:
                if nextop == LOAD_FAST:
                    make_super_instruction(inst, b.instr[i + 1], STORE_FAST_LOAD_FAST)
                elif nextop == STORE_FAST:
                    make_super_instruction(inst, b.instr[i + 1], STORE_FAST_STORE_FAST)
    remove_redundant_nops(g)


NOT_LOCAL = -1
DUMMY_INSTR = -1

SUPPORT_KILLED = 1
STORED_AS_LOCAL = 2
REF_UNCONSUMED = 4


def kill_local(instr_flags, refs, local):
    for (rinstr, rlocal) in refs:
        if rlocal == local:
            assert rinstr >= 0
            instr_flags[rinstr] |= SUPPORT_KILLED


def store_local(instr_flags, refs, local, r):
    kill_local(instr_flags, refs, local)
    if r[0] != DUMMY_INSTR:
        instr_flags[r[0]] |= STORED_AS_LOCAL


def load_fast_push_block(sp, target, start_depth):
    assert target.startdepth >= 0 and target.startdepth == start_depth, \
        (target.startdepth, start_depth)
    if not target.visited:
        target.visited = 1
        sp.append(target)


_CONSUME_NONE = frozenset([FORMAT_SIMPLE, GET_ANEXT, GET_LEN, GET_YIELD_FROM_ITER, IMPORT_FROM,
                           MATCH_KEYS, MATCH_MAPPING, MATCH_SEQUENCE, WITH_EXCEPT_START])
_PUSH_NONE = frozenset([DICT_MERGE, DICT_UPDATE, LIST_APPEND, LIST_EXTEND, MAP_ADD, RERAISE,
                        SET_ADD, SET_UPDATE])


def optimize_load_fast(g):
    entryblock = g.entryblock
    sp = make_cfg_traversal_stack(entryblock)
    sp.append(entryblock)
    entryblock.startdepth = 0
    entryblock.visited = 1
    while sp:
        block = sp.pop()
        assert block.startdepth > -1
        instr_flags = [0] * block.iused
        refs = []
        for _ in range(block.startdepth):
            refs.append((DUMMY_INSTR, NOT_LOCAL))
        for i in range(block.iused):
            instr = block.instr[i]
            opcode = instr.opcode
            oparg = instr.oparg
            assert opcode != EXTENDED_ARG
            if opcode == DELETE_FAST:
                kill_local(instr_flags, refs, oparg)
            elif opcode == LOAD_FAST:
                refs.append((i, oparg))
            elif opcode == LOAD_FAST_AND_CLEAR:
                kill_local(instr_flags, refs, oparg)
                refs.append((i, oparg))
            elif opcode == LOAD_FAST_LOAD_FAST:
                refs.append((i, oparg >> 4))
                refs.append((i, oparg & 15))
            elif opcode == STORE_FAST:
                r = refs.pop()
                store_local(instr_flags, refs, oparg, r)
            elif opcode == STORE_FAST_LOAD_FAST:
                r = refs.pop()
                store_local(instr_flags, refs, oparg >> 4, r)
                refs.append((i, oparg & 15))
            elif opcode == STORE_FAST_STORE_FAST:
                r = refs.pop()
                store_local(instr_flags, refs, oparg >> 4, r)
                r = refs.pop()
                store_local(instr_flags, refs, oparg & 15, r)
            elif opcode == COPY:
                assert oparg > 0
                r = refs[len(refs) - oparg]
                refs.append(r)
            elif opcode == SWAP:
                assert oparg >= 2
                idx = len(refs) - oparg
                refs[idx], refs[-1] = refs[-1], refs[idx]
            elif opcode in _CONSUME_NONE:
                num_popped = op.num_popped(opcode, oparg)
                num_pushed = op.num_pushed(opcode, oparg)
                net_pushed = num_pushed - num_popped
                assert net_pushed >= 0
                # NB: the C loop `for (int i = 0; i < net_pushed; i++)
                # PUSH_REF(i, NOT_LOCAL)` shadows the instruction index, so
                # the pushed refs point at instructions 0, 1, ... of the
                # block.  Reproduced faithfully.
                for j in range(net_pushed):
                    refs.append((j, NOT_LOCAL))
            elif opcode in _PUSH_NONE:
                num_popped = op.num_popped(opcode, oparg)
                num_pushed = op.num_pushed(opcode, oparg)
                net_popped = num_popped - num_pushed
                assert net_popped > 0
                for _ in range(net_popped):
                    refs.pop()
            elif opcode in (END_SEND, SET_FUNCTION_ATTRIBUTE):
                tos = refs.pop()
                refs.pop()
                refs.append(tos)
            elif opcode == CHECK_EXC_MATCH:
                refs.pop()
                refs.append((i, NOT_LOCAL))
            elif opcode == FOR_ITER:
                load_fast_push_block(sp, instr.target, len(refs) + 1)
                refs.append((i, NOT_LOCAL))
            elif opcode in (LOAD_ATTR, LOAD_SUPER_ATTR):
                self_ = refs.pop()
                if opcode == LOAD_SUPER_ATTR:
                    refs.pop()
                    refs.pop()
                refs.append((i, NOT_LOCAL))
                if oparg & 1:
                    refs.append(self_)
            elif opcode in (LOAD_SPECIAL, PUSH_EXC_INFO):
                tos = refs.pop()
                refs.append((i, NOT_LOCAL))
                refs.append(tos)
            elif opcode == SEND:
                load_fast_push_block(sp, instr.target, len(refs))
                refs.pop()
                refs.append((i, NOT_LOCAL))
            else:
                num_popped = op.num_popped(opcode, oparg)
                num_pushed = op.num_pushed(opcode, oparg)
                if op.has_target(instr.opcode):
                    load_fast_push_block(sp, instr.target, len(refs) - num_popped + num_pushed)
                if not op.is_block_push_opcode(instr.opcode):
                    for _ in range(num_popped):
                        refs.pop()
                    for _ in range(num_pushed):
                        refs.append((i, NOT_LOCAL))
        term = block.last_instr()
        if (term is not None and block.next is not None
                and not (op.is_unconditional_jump_opcode(term.opcode)
                         or op.is_scope_exit_opcode(term.opcode))):
            assert BB_HAS_FALLTHROUGH(block)
            load_fast_push_block(sp, block.next, len(refs))
        for (rinstr, rlocal) in refs:
            if rinstr != -1:
                instr_flags[rinstr] |= REF_UNCONSUMED
        for i in range(block.iused):
            if not instr_flags[i]:
                instr = block.instr[i]
                if instr.opcode == LOAD_FAST:
                    instr.opcode = LOAD_FAST_BORROW
                elif instr.opcode == LOAD_FAST_LOAD_FAST:
                    instr.opcode = LOAD_FAST_BORROW_LOAD_FAST_BORROW


def maybe_push(b, unsafe_mask, sp):
    both = b.unsafe_locals_mask | unsafe_mask
    if b.unsafe_locals_mask != both:
        b.unsafe_locals_mask = both
        if not b.visited:
            sp.append(b)
            b.visited = 1


def scan_block_for_locals(b, sp):
    unsafe_mask = b.unsafe_locals_mask
    for instr in b.instr:
        assert instr.opcode != EXTENDED_ARG
        if instr.except_ is not None:
            maybe_push(instr.except_, unsafe_mask, sp)
        if instr.oparg >= 64:
            continue
        assert instr.oparg >= 0
        bit = 1 << instr.oparg
        opcode = instr.opcode
        if opcode in (DELETE_FAST, LOAD_FAST_AND_CLEAR, STORE_FAST_MAYBE_NULL):
            unsafe_mask |= bit
        elif opcode == STORE_FAST:
            unsafe_mask &= ~bit
        elif opcode == LOAD_FAST_CHECK:
            unsafe_mask &= ~bit
        elif opcode == LOAD_FAST:
            if unsafe_mask & bit:
                instr.opcode = LOAD_FAST_CHECK
            unsafe_mask &= ~bit
    if b.next is not None and BB_HAS_FALLTHROUGH(b):
        maybe_push(b.next, unsafe_mask, sp)
    last = b.last_instr()
    if last is not None and is_jump(last):
        assert last.target is not None
        maybe_push(last.target, unsafe_mask, sp)


def fast_scan_many_locals(entryblock, nlocals):
    assert nlocals > 64
    states = [0] * (nlocals - 64)
    blocknum = 0
    for b in iter_blocks(entryblock):
        blocknum += 1
        for instr in b.instr:
            assert instr.opcode != EXTENDED_ARG
            arg = instr.oparg
            if arg < 64:
                continue
            opcode = instr.opcode
            if opcode in (DELETE_FAST, LOAD_FAST_AND_CLEAR, STORE_FAST_MAYBE_NULL):
                states[arg - 64] = blocknum - 1
            elif opcode == STORE_FAST:
                states[arg - 64] = blocknum
            elif opcode == LOAD_FAST:
                if states[arg - 64] != blocknum:
                    instr.opcode = LOAD_FAST_CHECK
                states[arg - 64] = blocknum


def remove_unused_consts(entryblock, consts):
    nconsts = len(consts)
    if nconsts == 0:
        return
    index_map = [-1] * nconsts
    index_map[0] = 0
    for b in iter_blocks(entryblock):
        for instr in b.instr:
            if op.has_const(instr.opcode):
                index = instr.oparg
                index_map[index] = index
    n_used_consts = 0
    for i in range(nconsts):
        if index_map[i] != -1:
            assert index_map[i] == i
            index_map[n_used_consts] = index_map[i]
            n_used_consts += 1
    if n_used_consts == nconsts:
        return
    assert n_used_consts < nconsts
    for i in range(n_used_consts):
        old_index = index_map[i]
        assert i <= old_index < nconsts
        if i != old_index:
            consts[i] = consts[index_map[i]]
    del consts[n_used_consts:]
    reverse_index_map = [-1] * nconsts
    for i in range(n_used_consts):
        assert index_map[i] != -1
        assert reverse_index_map[index_map[i]] == -1
        reverse_index_map[index_map[i]] = i
    for b in iter_blocks(entryblock):
        for instr in b.instr:
            if op.has_const(instr.opcode):
                index = instr.oparg
                assert 0 <= reverse_index_map[index] < n_used_consts
                instr.oparg = reverse_index_map[index]


def add_checks_for_loads_of_uninitialized_variables(entryblock, nlocals, nparams):
    if nlocals == 0:
        return
    if nlocals > 64:
        fast_scan_many_locals(entryblock, nlocals)
        nlocals = 64
    sp = make_cfg_traversal_stack(entryblock)
    start_mask = 0
    for i in range(nparams, nlocals):
        start_mask |= 1 << i
    maybe_push(entryblock, start_mask, sp)
    for b in iter_blocks(entryblock):
        scan_block_for_locals(b, sp)
    while sp:
        b = sp.pop()
        b.visited = 0
        scan_block_for_locals(b, sp)


def mark_warm(entryblock):
    sp = make_cfg_traversal_stack(entryblock)
    sp.append(entryblock)
    entryblock.visited = 1
    while sp:
        b = sp.pop()
        assert not b.except_handler
        b.warm = 1
        nxt = b.next
        if nxt is not None and BB_HAS_FALLTHROUGH(b) and not nxt.visited:
            sp.append(nxt)
            nxt.visited = 1
        for instr in b.instr:
            if is_jump(instr) and not instr.target.visited:
                sp.append(instr.target)
                instr.target.visited = 1


def mark_cold(entryblock):
    for b in iter_blocks(entryblock):
        assert not b.cold and not b.warm
    mark_warm(entryblock)
    sp = make_cfg_traversal_stack(entryblock)
    for b in iter_blocks(entryblock):
        if b.except_handler:
            assert not b.warm
            sp.append(b)
            b.visited = 1
    while sp:
        b = sp.pop()
        b.cold = 1
        nxt = b.next
        if nxt is not None and BB_HAS_FALLTHROUGH(b):
            if not nxt.warm and not nxt.visited:
                sp.append(nxt)
                nxt.visited = 1
        for i, instr in enumerate(b.instr):
            if is_jump(instr):
                assert i == b.iused - 1
                target = instr.target
                if not target.warm and not target.visited:
                    sp.append(target)
                    target.visited = 1


def push_cold_blocks_to_end(g):
    entryblock = g.entryblock
    if entryblock.next is None:
        return
    mark_cold(entryblock)
    next_lbl = get_max_label(g.entryblock) + 1
    for b in iter_blocks(entryblock):
        if b.cold and BB_HAS_FALLTHROUGH(b) and b.next is not None and b.next.warm:
            explicit_jump = g.new_block()
            if b.next.label == NO_LABEL:
                b.next.label = next_lbl
                next_lbl += 1
            basicblock_addop(explicit_jump, JUMP_NO_INTERRUPT, b.next.label, NO_LOCATION)
            explicit_jump.cold = 1
            explicit_jump.next = b.next
            explicit_jump.predecessors = 1
            b.next = explicit_jump
            last = explicit_jump.last_instr()
            last.target = explicit_jump.next
    assert not entryblock.cold
    cold_blocks = None
    cold_blocks_tail = None
    b = entryblock
    while b.next is not None:
        assert not b.cold
        while b.next is not None and not b.next.cold:
            b = b.next
        if b.next is None:
            break
        assert not b.cold and b.next.cold
        b_end = b.next
        while b_end.next is not None and b_end.next.cold:
            b_end = b_end.next
        assert b_end is not None and b_end.cold
        assert b_end.next is None or not b_end.next.cold
        if cold_blocks is None:
            cold_blocks = b.next
        else:
            cold_blocks_tail.next = b.next
        cold_blocks_tail = b_end
        b.next = b_end.next
        b_end.next = None
    assert b is not None and b.next is None
    b.next = cold_blocks
    if cold_blocks is not None:
        remove_redundant_nops_and_jumps(g)


def convert_pseudo_conditional_jumps(g):
    for b in iter_blocks(g.entryblock):
        i = 0
        while i < b.iused:
            instr = b.instr[i]
            if instr.opcode in (JUMP_IF_FALSE, JUMP_IF_TRUE):
                assert i == b.iused - 1
                instr.opcode = POP_JUMP_IF_FALSE if instr.opcode == JUMP_IF_FALSE else POP_JUMP_IF_TRUE
                loc = instr.loc
                except_ = instr.except_
                b.instr.insert(i, CfgInstr(COPY, 1, loc, None, except_))
                i += 1
                b.instr.insert(i, CfgInstr(TO_BOOL, 0, loc, None, except_))
                i += 1
                b.grew()
            i += 1


def convert_pseudo_ops(g):
    for b in iter_blocks(g.entryblock):
        for instr in b.instr:
            if is_block_push(instr):
                instr.set_op0(NOP)
            elif instr.opcode == LOAD_CLOSURE:
                instr.opcode = LOAD_FAST
            elif instr.opcode == STORE_FAST_MAYBE_NULL:
                instr.opcode = STORE_FAST
    remove_redundant_nops_and_jumps(g)


def is_exit_or_eval_check_without_lineno(b):
    if basicblock_exits_scope(b) or basicblock_has_eval_break(b):
        return basicblock_has_no_lineno(b)
    return False


def duplicate_exits_without_lineno(g):
    next_lbl = get_max_label(g.entryblock) + 1
    entryblock = g.entryblock
    for b in iter_blocks(entryblock):
        last = b.last_instr()
        if last is None:
            continue
        if is_jump(last):
            target = next_nonempty_block(last.target)
            if is_exit_or_eval_check_without_lineno(target) and target.predecessors > 1:
                new_target = g.copy_basicblock(target)
                new_target.instr[0].loc = last.loc
                last.target = new_target
                target.predecessors -= 1
                new_target.predecessors = 1
                new_target.next = target.next
                new_target.label = next_lbl
                next_lbl += 1
                target.next = new_target
    for b in iter_blocks(entryblock):
        if BB_HAS_FALLTHROUGH(b) and b.next is not None and b.iused > 0:
            if is_exit_or_eval_check_without_lineno(b.next):
                last = b.last_instr()
                assert last is not None
                b.next.instr[0].loc = last.loc


def propagate_line_numbers(entryblock):
    for b in iter_blocks(entryblock):
        last = b.last_instr()
        if last is None:
            continue
        prev_location = NO_LOCATION
        for instr in b.instr:
            if instr.loc.lineno == NO_LOCATION.lineno:
                instr.loc = prev_location
            else:
                prev_location = instr.loc
        if BB_HAS_FALLTHROUGH(b) and b.next.predecessors == 1:
            if b.next.iused > 0:
                if b.next.instr[0].loc.lineno == NO_LOCATION.lineno:
                    b.next.instr[0].loc = prev_location
        if is_jump(last):
            target = last.target
            if target.predecessors == 1:
                first = target.first_instr_even_if_stale()
                if first is not None and first.loc.lineno == NO_LOCATION.lineno:
                    first.loc = prev_location


def resolve_line_numbers(g, firstlineno):
    duplicate_exits_without_lineno(g)
    propagate_line_numbers(g.entryblock)


def optimize_code_unit(g, consts, const_cache, nlocals, nparams, firstlineno):
    """_PyCfg_OptimizeCodeUnit."""
    assert g.entryblock.iused > 0
    translate_jump_labels_to_targets(g.entryblock)
    mark_except_handlers(g.entryblock)
    label_exception_targets(g.entryblock)
    consts_index = {}
    for i, item in enumerate(consts):
        if id(item) in consts_index:
            continue
        consts_index[id(item)] = i
    optimize_cfg(g, consts, const_cache, consts_index, firstlineno)
    remove_unused_consts(g.entryblock, consts)
    add_checks_for_loads_of_uninitialized_variables(g.entryblock, nlocals, nparams)
    insert_superinstructions(g)
    push_cold_blocks_to_end(g)
    resolve_line_numbers(g, firstlineno)


def build_cellfixedoffsets(umd):
    nlocals = len(umd.u_varnames)
    ncellvars = len(umd.u_cellvars)
    nfreevars = len(umd.u_freevars)
    noffsets = ncellvars + nfreevars
    fixed = [nlocals + i for i in range(noffsets)]
    for varname, cellindex in umd.u_cellvars.items():
        varindex = umd.u_varnames.get(varname)
        if varindex is None:
            continue
        fixed[cellindex] = varindex
    return fixed


def IS_GENERATOR(cf):
    return cf & (CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR)


def insert_prefix_instructions(umd, entryblock, fixed, nfreevars, code_flags):
    assert umd.u_firstlineno > 0
    if IS_GENERATOR(code_flags):
        loc = LOCATION(umd.u_firstlineno, umd.u_firstlineno, -1, -1)
        entryblock.instr.insert(0, CfgInstr(RETURN_GENERATOR, 0, loc))
        entryblock.instr.insert(1, CfgInstr(POP_TOP, 0, loc))
        entryblock.grew()
    ncellvars = len(umd.u_cellvars)
    if ncellvars:
        nvars = ncellvars + len(umd.u_varnames)
        sorted_ = [0] * nvars
        for i in range(ncellvars):
            sorted_[fixed[i]] = i + 1
        ncellsused = 0
        i = 0
        while ncellsused < ncellvars:
            oldindex = sorted_[i] - 1
            i += 1
            if oldindex == -1:
                continue
            entryblock.instr.insert(ncellsused, CfgInstr(MAKE_CELL, oldindex, NO_LOCATION))
            entryblock.grew()
            ncellsused += 1
    if nfreevars:
        entryblock.instr.insert(0, CfgInstr(COPY_FREE_VARS, nfreevars, NO_LOCATION))
        entryblock.grew()


def fix_cell_offsets(umd, entryblock, fixedmap):
    nlocals = len(umd.u_varnames)
    ncellvars = len(umd.u_cellvars)
    nfreevars = len(umd.u_freevars)
    noffsets = ncellvars + nfreevars
    numdropped = 0
    for i in range(noffsets):
        if fixedmap[i] == i + nlocals:
            fixedmap[i] -= numdropped
        else:
            numdropped += 1
    for b in iter_blocks(entryblock):
        for inst in b.instr:
            assert inst.opcode != EXTENDED_ARG
            oldoffset = inst.oparg
            if inst.opcode in (MAKE_CELL, LOAD_CLOSURE, LOAD_DEREF, STORE_DEREF, DELETE_DEREF,
                               LOAD_FROM_DICT_OR_DEREF):
                assert 0 <= oldoffset < noffsets
                assert fixedmap[oldoffset] >= 0
                inst.oparg = fixedmap[oldoffset]
    return numdropped


def prepare_localsplus(umd, g, code_flags):
    nlocals = len(umd.u_varnames)
    ncellvars = len(umd.u_cellvars)
    nfreevars = len(umd.u_freevars)
    nlocalsplus = nlocals + ncellvars + nfreevars
    cellfixedoffsets = build_cellfixedoffsets(umd)
    insert_prefix_instructions(umd, g.entryblock, cellfixedoffsets, nfreevars, code_flags)
    numdropped = fix_cell_offsets(umd, g.entryblock, cellfixedoffsets)
    nlocalsplus -= numdropped
    return nlocalsplus


def cfg_from_instruction_sequence(seq):
    """_PyCfg_FromInstructionSequence."""
    seq.apply_label_map()
    g = CfgBuilder()
    n = len(seq.instrs)
    is_target = [False] * n
    for instr in seq.instrs:
        if op.has_target(instr.opcode):
            assert 0 <= instr.oparg < n
            is_target[instr.oparg] = True
    offset = 0
    for i, instr in enumerate(seq.instrs):
        if instr.opcode == ANNOTATIONS_PLACEHOLDER:
            if seq.annotations_code is not None:
                ann = seq.annotations_code
                assert not ann.labelmap and not ann.nested
                for ann_instr in ann.instrs:
                    assert not op.has_target(ann_instr.opcode)
                    g.addop(ann_instr.opcode, ann_instr.oparg, ann_instr.loc)
                offset += len(ann.instrs) - 1
            else:
                offset -= 1
            continue
        if is_target[i]:
            g.use_label(i + offset)
        opcode = instr.opcode
        oparg = instr.oparg
        if op.has_target(opcode):
            oparg += offset
        g.addop(opcode, oparg, instr.loc)
    return g


def cfg_to_instruction_sequence(g, seq):
    """_PyCfg_ToInstructionSequence."""
    lbl = 0
    for b in iter_blocks(g.entryblock):
        b.label = lbl
        lbl += 1
    for b in iter_blocks(g.entryblock):
        seq.use_label(b.label)
        for instr in b.instr:
            if op.has_target(instr.opcode):
                instr.oparg = instr.target.label
            seq.addop(instr.opcode, instr.oparg, instr.loc)
            hi = seq.instrs[-1]
            if instr.except_ is not None:
                hi.except_handler_label = instr.except_.label
                hi.except_handler_startdepth = instr.except_.startdepth
                hi.except_handler_preserve_lasti = instr.except_.preserve_lasti
            else:
                hi.except_handler_label = -1
    seq.apply_label_map()


def optimized_cfg_to_instruction_sequence(g, umd, code_flags):
    """_PyCfg_OptimizedCfgToInstructionSequence: returns
    (stackdepth, nlocalsplus, seq)."""
    convert_pseudo_conditional_jumps(g)
    stackdepth = calculate_stackdepth(g)
    assert not (IS_GENERATOR(code_flags) and stackdepth == 0)
    nlocalsplus = prepare_localsplus(umd, g, code_flags)
    convert_pseudo_ops(g)
    normalize_jumps(g)
    optimize_load_fast(g)
    seq = InstructionSequence()
    cfg_to_instruction_sequence(g, seq)
    return stackdepth, nlocalsplus, seq
