"""Token type constants for the CPython 3.14 tokenizer.

Mirrors Lib/token.py (auto-generated from Grammar/Tokens in CPython).
"""

ENDMARKER = 0
NAME = 1
NUMBER = 2
STRING = 3
NEWLINE = 4
INDENT = 5
DEDENT = 6
LPAR = 7
RPAR = 8
LSQB = 9
RSQB = 10
COLON = 11
COMMA = 12
SEMI = 13
PLUS = 14
MINUS = 15
STAR = 16
SLASH = 17
VBAR = 18
AMPER = 19
LESS = 20
GREATER = 21
EQUAL = 22
DOT = 23
PERCENT = 24
LBRACE = 25
RBRACE = 26
EQEQUAL = 27
NOTEQUAL = 28
LESSEQUAL = 29
GREATEREQUAL = 30
TILDE = 31
CIRCUMFLEX = 32
LEFTSHIFT = 33
RIGHTSHIFT = 34
DOUBLESTAR = 35
PLUSEQUAL = 36
MINEQUAL = 37
STAREQUAL = 38
SLASHEQUAL = 39
PERCENTEQUAL = 40
AMPEREQUAL = 41
VBAREQUAL = 42
CIRCUMFLEXEQUAL = 43
LEFTSHIFTEQUAL = 44
RIGHTSHIFTEQUAL = 45
DOUBLESTAREQUAL = 46
DOUBLESLASH = 47
DOUBLESLASHEQUAL = 48
AT = 49
ATEQUAL = 50
RARROW = 51
ELLIPSIS = 52
COLONEQUAL = 53
EXCLAMATION = 54
OP = 55
TYPE_IGNORE = 56
TYPE_COMMENT = 57
SOFT_KEYWORD = 58
FSTRING_START = 59
FSTRING_MIDDLE = 60
FSTRING_END = 61
TSTRING_START = 62
TSTRING_MIDDLE = 63
TSTRING_END = 64
COMMENT = 65
NL = 66
ERRORTOKEN = 67
ENCODING = 68
N_TOKENS = 69
NT_OFFSET = 256

tok_name = {value: name
            for name, value in globals().items()
            if isinstance(value, int) and not name.startswith('_')}

EXACT_TOKEN_TYPES = {
    '!': EXCLAMATION,
    '!=': NOTEQUAL,
    '%': PERCENT,
    '%=': PERCENTEQUAL,
    '&': AMPER,
    '&=': AMPEREQUAL,
    '(': LPAR,
    ')': RPAR,
    '*': STAR,
    '**': DOUBLESTAR,
    '**=': DOUBLESTAREQUAL,
    '*=': STAREQUAL,
    '+': PLUS,
    '+=': PLUSEQUAL,
    ',': COMMA,
    '-': MINUS,
    '-=': MINEQUAL,
    '->': RARROW,
    '.': DOT,
    '...': ELLIPSIS,
    '/': SLASH,
    '//': DOUBLESLASH,
    '//=': DOUBLESLASHEQUAL,
    '/=': SLASHEQUAL,
    ':': COLON,
    ':=': COLONEQUAL,
    ';': SEMI,
    '<': LESS,
    '<<': LEFTSHIFT,
    '<<=': LEFTSHIFTEQUAL,
    '<=': LESSEQUAL,
    '=': EQUAL,
    '==': EQEQUAL,
    '>': GREATER,
    '>=': GREATEREQUAL,
    '>>': RIGHTSHIFT,
    '>>=': RIGHTSHIFTEQUAL,
    '@': AT,
    '@=': ATEQUAL,
    '[': LSQB,
    ']': RSQB,
    '^': CIRCUMFLEX,
    '^=': CIRCUMFLEXEQUAL,
    '{': LBRACE,
    '|': VBAR,
    '|=': VBAREQUAL,
    '}': RBRACE,
    '~': TILDE,
}

# Byte-keyed lookup tables mirroring Parser/token.c
ONE_CHAR = {ord(k): v for k, v in EXACT_TOKEN_TYPES.items() if len(k) == 1}
TWO_CHARS = {(ord(k[0]), ord(k[1])): v for k, v in EXACT_TOKEN_TYPES.items() if len(k) == 2}
TWO_CHARS[(ord('<'), ord('>'))] = NOTEQUAL   # barry_as_FLUFL
THREE_CHARS = {(ord(k[0]), ord(k[1]), ord(k[2])): v
               for k, v in EXACT_TOKEN_TYPES.items() if len(k) == 3}


def ISTERMINAL(x):
    return x < NT_OFFSET


def ISNONTERMINAL(x):
    return x >= NT_OFFSET


def ISEOF(x):
    return x == ENDMARKER


def is_string_lit(t):
    return t == STRING or t == FSTRING_MIDDLE or t == TSTRING_MIDDLE
