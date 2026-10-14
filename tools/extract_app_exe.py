#!/usr/bin/env python3
"""Carve `Insta360 Link Controller.exe` out of the Windows installer.

The installer is Inno Setup 6.3, which innoextract 1.9 (the usual distro
package) cannot read. Rather than build innoextract from source, this reads the
payload directly: the installer carries a `zlb\\x1a` header followed by a raw
LZMA1 stream (one properties byte, then a 4-byte dictionary size), and the
decompressed stream contains every packaged file back to back.

The app exe is identified by being the first MZ whose PE header checks out and
whose size, computed from the section table, is over 150 MB. Decompression is
streaming, so this needs ~a few MB of RAM rather than the 1.25 GB the full
payload would take.

Decompressing is not enough: Inno Setup also rewrites every `CALL rel32` /
`JMP rel32` operand to an absolute address before compressing, so the carve is
run through `inno_exe_unfilter` and then checked against the PE checksum and the
Authenticode digest. See tools/verify_pe.py for why both checks matter.

Usage: extract_app_exe.py <installer.exe> <output.exe>
"""
import lzma
import os
import struct
import sys

from inno_exe_unfilter import unfilter_file

ZLB_MAGIC = b"zlb\x1a"
MIN_APP_SIZE = 150 << 20
CHUNK = 1 << 22


def find_zlb(fh):
    """Offset of the first zlb block's compressed data, plus its LZMA filter."""
    fh.seek(0)
    tail = b""
    base = 0
    while True:
        buf = fh.read(CHUNK)
        if not buf:
            raise SystemExit("no zlb\\x1a block found")
        hay = tail + buf
        idx = hay.find(ZLB_MAGIC)
        if idx >= 0 and len(hay) - idx >= 9:
            off = base - len(tail) + idx
            props = hay[idx + 4]
            dict_size = struct.unpack_from("<I", hay, idx + 5)[0]
            pb, r = divmod(props, 45)
            lp, lc = divmod(r, 9)
            return off + 9, {
                "id": lzma.FILTER_LZMA1,
                "lc": lc,
                "lp": lp,
                "pb": pb,
                "dict_size": dict_size,
            }
        base += len(buf)
        tail = hay[-16:]


def pe_image_size(hdr, off):
    """Size on disk of the PE at `off` within `hdr`, or None if not a PE."""
    if hdr[off:off + 2] != b"MZ" or len(hdr) < off + 0x40:
        return None
    e_lfanew = struct.unpack_from("<I", hdr, off + 0x3C)[0]
    pe = off + e_lfanew
    if e_lfanew > 0x400 or len(hdr) < pe + 0x18:
        return None
    if hdr[pe:pe + 4] != b"PE\0\0":
        return None
    nsections, = struct.unpack_from("<H", hdr, pe + 6)
    opt_size, = struct.unpack_from("<H", hdr, pe + 20)
    sect = pe + 24 + opt_size
    if not 0 < nsections < 96 or len(hdr) < sect + nsections * 40:
        return None
    end = sect - off
    for i in range(nsections):
        s = sect + i * 40
        raw_ptr, = struct.unpack_from("<I", hdr, s + 20)
        raw_size, = struct.unpack_from("<I", hdr, s + 16)
        if raw_ptr:
            end = max(end, raw_ptr + raw_size)
    # The Authenticode signature is an overlay past the last section, and the
    # security directory addresses it by file offset rather than by RVA. Omit it
    # and the file still disassembles, but neither integrity check can run.
    opt = pe + 24
    ndirs, = struct.unpack_from("<I", hdr, opt + 108)
    if ndirs > 4:
        cert_off, cert_size = struct.unpack_from("<II", hdr, opt + 112 + 4 * 8)
        if cert_off and cert_size:
            end = max(end, cert_off + cert_size)
    return end


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    installer, out_path = sys.argv[1], sys.argv[2]
    raw_path = out_path + ".filtered"
    fh = open(installer, "rb")
    data_off, flt = find_zlb(fh)
    print(f"zlb payload at {data_off:#x}, filter {flt}", file=sys.stderr)
    fh.seek(data_off)

    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[flt])
    buf = bytearray()
    produced = 0          # decompressed bytes consumed out of `buf` already
    out = None
    remaining = 0

    while True:
        raw = fh.read(CHUNK)
        if not raw:
            break
        try:
            chunk = dec.decompress(raw)
        except lzma.LZMAError as exc:
            print(f"stream ended: {exc}", file=sys.stderr)
            break
        if not chunk:
            continue
        if out is not None:
            take = chunk[:remaining]
            out.write(take)
            remaining -= len(take)
            if remaining == 0:
                break
            continue
        buf += chunk
        # Keep only a window large enough to hold an MZ + its PE headers.
        scan = 0
        while True:
            idx = buf.find(b"MZ", scan)
            if idx < 0 or len(buf) - idx < 0x1000:
                break
            size = pe_image_size(buf, idx)
            if size and size >= MIN_APP_SIZE:
                print(f"app exe at payload offset {produced + idx:#x}, "
                      f"{size} bytes", file=sys.stderr)
                out = open(raw_path, "wb")
                out.write(buf[idx:])
                remaining = size - (len(buf) - idx)
                buf = bytearray()
                break
            scan = idx + 1
        if out is None and len(buf) > (1 << 20):
            keep = 1 << 16
            produced += len(buf) - keep
            del buf[:-keep]

    if out is None:
        raise SystemExit("no app exe found in payload")
    out.close()
    if remaining:
        raise SystemExit(f"truncated: {remaining} bytes short")

    converted = unfilter_file(raw_path, out_path)
    os.unlink(raw_path)
    print(f"wrote {out_path} ({converted} call/jmp operands un-filtered)",
          file=sys.stderr)
    print("now check it: tools/verify_pe.py " + out_path, file=sys.stderr)


if __name__ == "__main__":
    main()
