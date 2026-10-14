#!/usr/bin/env python3
"""Find a class's vtable in the app exe via MSVC RTTI, and print its slots.

The binary keeps RTTI, so the vtable of a named class can be found exactly
rather than guessed: the mangled name lives in a type descriptor, a complete
object locator points at that descriptor by RVA, and a vtable's slot -1 points
at the locator. Virtual calls in the disassembly look like

    mov rcx, [rcx] ; mov rax, [rcx] ; jmp [rax + 0x30]

so the slot index is the displacement over 8.

Usage: vtable.py <app.exe> <ClassName> [slot]
"""
import struct
import sys

sys.path.insert(0, __file__.rsplit('/', 2)[0] + '/re')


def sections(fh):
    fh.seek(0)
    d = fh.read(0x1000)
    e = struct.unpack_from('<I', d, 0x3C)[0]
    nsect, = struct.unpack_from('<H', d, e + 6)
    optsz, = struct.unpack_from('<H', d, e + 20)
    opt = e + 24
    base, = struct.unpack_from('<Q', d, opt + 24)
    out = []
    for i in range(nsect):
        s = opt + optsz + i * 40
        name = d[s:s + 8].rstrip(b'\0').decode('latin1')
        vsz, va, rsz, rptr = struct.unpack_from('<IIII', d, s + 8)
        out.append(dict(name=name, va=va, vsz=vsz, rptr=rptr, rsz=rsz))
    return base, out


def main():
    path, cls = sys.argv[1], sys.argv[2]
    slot = int(sys.argv[3], 0) if len(sys.argv) > 3 else None
    fh = open(path, 'rb')
    base, sects = sections(fh)

    def load(name):
        s = [x for x in sects if x['name'] == name][0]
        fh.seek(s['rptr'])
        return s, fh.read(s['rsz'])

    # .data holds the type descriptors, .rdata the locators and vtables.
    dsec, data = load('.data')
    rsec, rdata = load('.rdata')

    name = b'.?AV' + cls.encode() + b'@@\0'
    i = data.find(name)
    if i < 0:
        raise SystemExit(f'no type descriptor for {cls}')
    # A type descriptor is { vftable ptr, spare, name[] }.
    desc_rva = dsec['va'] + i - 16
    print(f'type descriptor rva {desc_rva:#x} ({base + desc_rva:#x})')

    # The complete object locator references the descriptor by RVA, at +12.
    key = struct.pack('<I', desc_rva)
    hits = []
    j = rdata.find(key)
    while j >= 0:
        hits.append(j)
        j = rdata.find(key, j + 1)
    for h in hits:
        col_rva = rsec['va'] + h - 12
        # A vtable's slot -1 points at the locator.
        vptr = struct.pack('<Q', base + col_rva)
        k = rdata.find(vptr)
        while k >= 0:
            vt = base + rsec['va'] + k + 8
            print(f'  locator {base + col_rva:#x} -> vtable {vt:#x}')
            if slot is not None:
                off = k + 8 + slot * 8
                fn, = struct.unpack_from('<Q', rdata, off)
                print(f'    slot {slot} (+{slot * 8:#x}) = {fn:#x}')
            else:
                for n in range(16):
                    fn, = struct.unpack_from('<Q', rdata, k + 8 + n * 8)
                    print(f'    slot {n:2d} (+{n * 8:#04x}) = {fn:#x}')
            k = rdata.find(vptr, k + 1)


if __name__ == '__main__':
    main()
