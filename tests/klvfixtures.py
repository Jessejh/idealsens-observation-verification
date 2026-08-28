"""Builders for synthetic GPMF payloads.

Real GoPro files are 4 GB each and cannot live in the repo, so the parser is
exercised against KLV assembled here to the same layout the camera writes.
"""

from __future__ import annotations

import struct


def klv(key: str, type_char: str, struct_size: int, repeat: int, payload: bytes) -> bytes:
    """One KLV record, padded to a 4-byte boundary like the camera writes it."""
    header = key.encode("latin-1") + type_char.encode("latin-1")
    header += bytes([struct_size]) + struct.pack(">H", repeat)
    return header + payload + b"\x00" * (-len(payload) % 4)


def nested(key: str, body: bytes) -> bytes:
    """A container KLV (DEVC, STRM): NUL type, size 1, repeat = body length."""
    return klv(key, "\x00", 1, len(body), body)


def text(key: str, value: str) -> bytes:
    raw = value.encode("latin-1")
    return klv(key, "c", len(raw), 1, raw)


def scal(*divisors: int) -> bytes:
    body = b"".join(struct.pack(">i", d) for d in divisors)
    return klv("SCAL", "l", 4, len(divisors), body)


def gps5(rows, scale=(10000000, 10000000, 1000, 1000, 1000)) -> bytes:
    """GPS5: lat, lon, alt, 2D speed, 3D speed as scaled int32."""
    body = b""
    for lat, lon, alt, s2, s3 in rows:
        body += struct.pack(">5i",
                            round(lat * scale[0]), round(lon * scale[1]),
                            round(alt * scale[2]), round(s2 * scale[3]),
                            round(s3 * scale[4]))
    return klv("GPS5", "l", 20, len(rows), body)


def gps9(rows, scale=(10000000, 10000000, 1000, 1000, 1000, 1, 1000, 100, 1)) -> bytes:
    """GPS9: seven int32 then two uint16, described by a TYPE of 'lllllllSS'."""
    body = b""
    for lat, lon, alt, s2, s3, days, secs, dop, fix in rows:
        body += struct.pack(">7iHH",
                            round(lat * scale[0]), round(lon * scale[1]),
                            round(alt * scale[2]), round(s2 * scale[3]),
                            round(s3 * scale[4]), round(days * scale[5]),
                            round(secs * scale[6]),
                            round(dop * scale[7]), round(fix * scale[8]))
    return klv("GPS9", "?", 32, len(rows), body)


def gpsu(stamp: str) -> bytes:
    raw = stamp.encode("latin-1")
    return klv("GPSU", "U", len(raw), 1, raw)


def gpsf(fix: int) -> bytes:
    return klv("GPSF", "L", 4, 1, struct.pack(">I", fix))


def gpsp(dop_x100: int) -> bytes:
    return klv("GPSP", "S", 2, 1, struct.pack(">H", dop_x100))


def gps5_payload(rows, stamp="170417105755.000", fix=3, dop_x100=180,
                 device="HERO5 Black") -> bytes:
    """A complete DEVC payload holding one GPS5 stream, as a HERO5 writes it."""
    stream = (text("STNM", "GPS (Lat., Long., Alt., 2D speed, 3D speed)")
              + scal(10000000, 10000000, 1000, 1000, 1000)
              + gpsf(fix) + gpsu(stamp) + gpsp(dop_x100) + gps5(rows))
    return nested("DEVC", klv("DVID", "L", 4, 1, struct.pack(">I", 1))
                  + text("DVNM", device) + nested("STRM", stream))


def gps9_payload(rows, device="HERO13 Black") -> bytes:
    stream = (text("STNM", "GPS (Lat., Long., Alt., 2D, 3D, days, secs, DOP, fix)")
              + scal(10000000, 10000000, 1000, 1000, 1000, 1, 1000, 100, 1)
              + text("TYPE", "lllllllSS") + gps9(rows))
    return nested("DEVC", text("DVNM", device) + nested("STRM", stream))
