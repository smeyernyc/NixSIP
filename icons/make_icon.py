#!/usr/bin/env python3
"""Generate a simple 22x22 PNG icon for NixSIP (phone/call style)."""
import struct
import zlib

def png_chunk(chunk_type, data):
    chunk = chunk_type + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xffffffff)

def make_png(w, h, r, g, b):
    # PNG signature
    signature = b"\x89PNG\r\n\x1a\n"
    # IHDR
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    # Raw image: row filter byte + row data (each row: filter 0 then w*3 bytes)
    raw = b""
    for y in range(h):
        raw += b"\x00"  # filter none
        for x in range(w):
            raw += bytes((r, g, b))
    # Zlib compress
    compressed = zlib.compress(raw, 9)
    ihdr_chunk = png_chunk(b"IHDR", ihdr)
    idat_chunk = png_chunk(b"IDAT", compressed)
    iend_chunk = png_chunk(b"IEND", b"")
    return signature + ihdr_chunk + idat_chunk + iend_chunk

if __name__ == "__main__":
    # Simple blue-green icon (SIP/phone feel)
    png = make_png(22, 22, 0, 120, 200)
    with open(__file__.replace("make_icon.py", "nixsip.png"), "wb") as f:
        f.write(png)
    print("Wrote nixsip.png")
