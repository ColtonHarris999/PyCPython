# Deviations from the oracle

The byte-exact oracle is `mpy-cross` built from MicroPython tag **v1.29.0**.
Everywhere the compiler's output differs from it, the difference is listed
here. Anything not listed is a bug.

## D1. Integer literals outside signed 64-bit

`mpy-cross` on the host is built with arbitrary-precision integers. The
target has no bigint: its widest integer is a signed 64-bit value. So the
oracle accepts literals the target must reject.

Measured at the pin:

| literal | `mpy-cross` v1.29.0 | this compiler |
|---|---|---|
| `1073741823` (2³⁰−1) | accepted, 33 B — small-int encoding | same |
| `1073741824` | accepted, 41 B — const pool | same |
| `9223372036854775807` (2⁶³−1) | accepted, 50 B — const pool | same |
| `9223372036854775808` (2⁶³) | **accepted**, 50 B | **compile error** |
| `2**200` | **accepted**, 92 B | **compile error** |

*Rule:* inside signed 64-bit, match the oracle's bytes exactly, including
the const-pool form for values that fit in 64 bits but not in the 31-bit
small int. Outside signed 64-bit, raise a compile error.

*Consequence for testing:* the harness treats "oracle accepted, we rejected"
as a **pass** for literals outside signed 64-bit. Without that, the suite
would enforce behaviour the target cannot implement.

Note the const pool stores numbers as their textual repr
(`save_obj` in `py/persistentcode.c`), so matching the oracle inside the
supported range needs no bigint of our own — only the decimal text.

## Non-deviations worth recording

These look like candidates and are not.

**The `.mpy` feature byte is `0x00`, not `0x03`.** `MPY_SUB_VERSION` is 3 at
the pin, and `mpy-cross` reports itself as emitting "mpy v6.3", but
`mp_raw_code_save` writes the sub-version into byte 2 only when
`cm->has_native` is set:

```c
(cm->arch_flags != 0 ? MPY_FEATURE_ARCH_FLAGS : 0) | (cm->has_native ?
    MPY_FEATURE_ENCODE_SUB_VERSION(MPY_SUB_VERSION) | MPY_FEATURE_ENCODE_ARCH(...) : 0),
```

A bytecode-only compile leaves `has_native` false, so the byte is `0x00` and
the full header is **`4d 06 00 1f`**. The loader checks the sub-version only
when the architecture nibble is not "none", so `0x00` and `0x03` both load —
but `0x00` is what the oracle writes, and byte-exactness is defined by the
oracle. Emitting `0x03` would miss on byte 3 of every file.

**The 32-bit register and the two-register wide value are invisible here.**
`n_state` counts Python values exactly as `mpy-cross` counts them; the target
maps a wide value onto two registers when the call starts. Emit stock
bytecode and ignore the register file.
