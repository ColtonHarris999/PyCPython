"""Port of Python/instruction_sequence.c: a growable list of pseudo
instructions with symbolic jump labels."""

from . import opcodes as op
from .errors import NO_LOCATION

NO_LABEL = -1


class Instruction:
    __slots__ = ('opcode', 'oparg', 'loc', 'target', 'offset', 'except_handler_label',
                 'except_handler_startdepth', 'except_handler_preserve_lasti')

    def __init__(self, opcode, oparg, loc):
        self.opcode = opcode
        self.oparg = oparg
        self.loc = loc
        self.target = None
        self.offset = 0
        self.except_handler_label = -1
        self.except_handler_startdepth = -1
        self.except_handler_preserve_lasti = 0

    def __repr__(self):
        return "<%s %r %r>" % (op.opname.get(self.opcode, self.opcode), self.oparg, self.loc)


class InstructionSequence:
    """_PyInstructionSequence."""

    def __init__(self):
        self.instrs = []
        self.next_free_label = 0
        self.labelmap = {}
        self.labelmap_applied = False
        self.nested = []
        self.annotations_code = None

    def new_label(self):
        self.next_free_label += 1
        return self.next_free_label

    def use_label(self, lbl):
        # label refers to the next instruction
        self.labelmap[lbl] = len(self.instrs)

    def apply_label_map(self):
        if self.labelmap_applied:
            return
        for instr in self.instrs:
            if op.has_target(instr.opcode):
                instr.oparg = self.labelmap[instr.oparg]
            if instr.except_handler_label >= 0:
                instr.except_handler_label = self.labelmap[instr.except_handler_label]
        self.labelmap = {}
        self.labelmap_applied = True

    def addop(self, opcode, oparg, loc):
        assert op.is_valid_opcode(opcode), opcode
        assert op.has_arg(opcode) or op.has_target(opcode) or oparg == 0, (opcode, oparg)
        assert 0 <= oparg < (1 << 30), oparg
        self.instrs.append(Instruction(opcode, oparg, loc))

    def insert_instruction(self, pos, opcode, oparg, loc):
        assert 0 <= pos <= len(self.instrs)
        self.instrs.insert(pos, Instruction(opcode, oparg, loc))
        for lbl, idx in self.labelmap.items():
            if idx >= pos:
                self.labelmap[lbl] = idx + 1

    def set_annotations_code(self, annotations):
        assert self.annotations_code is None
        self.annotations_code = annotations

    def add_nested(self, nested):
        self.nested.append(nested)

    def __len__(self):
        return len(self.instrs)
