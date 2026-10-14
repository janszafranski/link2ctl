#!/usr/bin/env python3
"""Undo the call-instruction transform Inno Setup applies to stored executables.

Inno Setup rewrites x86 `CALL rel32` / `JMP rel32` operands to be absolute
before compressing, because absolute targets repeat and therefore compress
better. The transform is lossless but it is *not* undone by decompression: a
naively carved payload is a structurally valid PE whose every long call and jump
points at the wrong address, which is why such a carve disassembles cleanly and
still cannot be followed.

This is the inverse of innoextract's `inno_exe_decoder_5200` with
`flip_high_byte` set, i.e. the encoding used by Inno Setup 5.3.9 and later.
Instructions are never allowed to span a 64 KiB block, so blocks are
independent and the pass streams.

Usage: inno_exe_unfilter.py <in.exe> <out.exe>
"""
import sys

BLOCK = 0x10000


def unfilter_block(buf, base):
    """In-place inverse transform of one block; `base` is its file offset."""
    n = len(buf)
    i = 0
    converted = 0
    while i < n:
        a = buf.find(b"\xe8", i)
        b = buf.find(b"\xe9", i)
        if a < 0 and b < 0:
            break
        j = b if a < 0 else (a if b < 0 else min(a, b))
        # The encoder skips any instruction that would cross the block end.
        if j + 5 > BLOCK or j + 5 > n:
            break
        high = buf[j + 4]
        # The encoder only rewrote operands whose high byte stayed a clean sign
        # extension; anything else was an 0xe8/0xe9 that is not an instruction.
        if high == 0x00 or high == 0xFF:
            absolute = buf[j + 1] | (buf[j + 2] << 8) | (buf[j + 3] << 16)
            rel = (absolute - ((base + j + 5) & 0xFFFFFF)) & 0xFFFFFFFF
            buf[j + 1] = rel & 0xFF
            buf[j + 2] = (rel >> 8) & 0xFF
            buf[j + 3] = (rel >> 16) & 0xFF
            if rel & 0x800000:
                # The encoder flipped the high byte so that backward jumps also
                # end up with a 0x00 high byte. Flip it back.
                buf[j + 4] = (~high) & 0xFF
            converted += 1
        i = j + 5
    return converted


def unfilter_file(src, dst):
    """Stream `src` to `dst` undoing the transform; returns operands converted."""
    converted = 0
    base = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            block = bytearray(fin.read(BLOCK))
            if not block:
                break
            converted += unfilter_block(block, base)
            fout.write(block)
            base += len(block)
    return converted


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    converted = unfilter_file(sys.argv[1], sys.argv[2])
    print(f"converted {converted} call/jmp operands", file=sys.stderr)


if __name__ == "__main__":
    main()
