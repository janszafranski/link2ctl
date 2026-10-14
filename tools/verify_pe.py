#!/usr/bin/env python3
"""Verify a carved PE is byte-identical to the signed original.

Two independent checks:

* the PE header's own CheckSum field, and
* the SHA-256 digest inside the Authenticode `SpcIndirectDataContent`, which is
  what the publisher actually signed.

Either one failing means the extraction is wrong, not merely incomplete: a
corrupt carve can still disassemble cleanly. Exits non-zero on mismatch.

Usage: verify_pe.py <file.exe>
"""
import hashlib
import struct
import sys

SHA256_OID = bytes.fromhex("608648016503040201")
SPC_INDIRECT_DATA_OID = bytes.fromhex("2b060104018237020104")
CHUNK = 1 << 22


def header_offsets(fh):
    fh.seek(0)
    d = fh.read(0x1000)
    e = struct.unpack_from("<I", d, 0x3C)[0]
    opt = e + 24
    checksum_off = opt + 64
    certdir_off = opt + 112 + 4 * 8
    stored_checksum, = struct.unpack_from("<I", d, checksum_off)
    cert_off, cert_size = struct.unpack_from("<II", d, certdir_off)
    return checksum_off, certdir_off, stored_checksum, cert_off, cert_size


def pe_checksum(fh, checksum_off, size):
    """The PE CheckSum algorithm: 16-bit ones-complement sum plus the file size."""
    fh.seek(0)
    total = 0
    pos = 0
    while True:
        buf = fh.read(CHUNK)
        if not buf:
            break
        b = bytearray(buf)
        if pos <= checksum_off < pos + len(b):
            for k in range(4):
                idx = checksum_off - pos + k
                if 0 <= idx < len(b):
                    b[idx] = 0
        if len(b) & 1:
            b.append(0)
        for v in struct.unpack("<%dH" % (len(b) // 2), bytes(b)):
            total += v
            total = (total & 0xFFFF) + (total >> 16)
        pos += len(buf)
    total = (total & 0xFFFF) + (total >> 16)
    total = (total + (total >> 16)) & 0xFFFF
    return (total + size) & 0xFFFFFFFF


def authenticode_digest(fh, checksum_off, certdir_off, cert_off):
    """SHA-256 over the file minus the checksum, the cert dir entry and the signature."""
    h = hashlib.sha256()

    def feed(start, end):
        fh.seek(start)
        left = end - start
        while left > 0:
            b = fh.read(min(left, CHUNK))
            if not b:
                break
            h.update(b)
            left -= len(b)

    feed(0, checksum_off)
    feed(checksum_off + 4, certdir_off)
    feed(certdir_off + 8, cert_off)
    return h.hexdigest()


def signed_digest(fh, cert_off, cert_size):
    """The digest the publisher signed, out of SpcIndirectDataContent."""
    fh.seek(cert_off)
    blob = fh.read(cert_size)[8:]  # skip the WIN_CERTIFICATE header
    spc = blob.find(SPC_INDIRECT_DATA_OID)
    if spc < 0:
        return None
    # DigestInfo follows: SEQUENCE { AlgorithmIdentifier { sha256, NULL },
    # OCTET STRING (32) }. Take the first sha256 after the content type OID.
    oid = blob.find(SHA256_OID, spc)
    if oid < 0:
        return None
    octet = blob.find(b"\x04\x20", oid, oid + 32)
    if octet < 0:
        return None
    return blob[octet + 2:octet + 34].hex()


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    fh = open(path, "rb")
    fh.seek(0, 2)
    size = fh.tell()
    checksum_off, certdir_off, stored, cert_off, cert_size = header_offsets(fh)

    ok = True

    computed = pe_checksum(fh, checksum_off, size)
    print(f"pe checksum      stored {stored:#x} computed {computed:#x} "
          f"{'MATCH' if computed == stored else 'MISMATCH'}")
    ok &= computed == stored

    if cert_off and cert_size:
        if cert_off + cert_size != size:
            print(f"signature table ends at {cert_off + cert_size:#x} but the file "
                  f"is {size:#x} bytes: wrong length")
            ok = False
        digest = authenticode_digest(fh, checksum_off, certdir_off, cert_off)
        signed = signed_digest(fh, cert_off, cert_size)
        print(f"authenticode     signed {signed} computed {digest} "
              f"{'MATCH' if signed == digest else 'MISMATCH'}")
        ok &= signed == digest
    else:
        print("no Authenticode signature present")
        ok = False

    print("VERIFIED" if ok else "NOT VERIFIED")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
