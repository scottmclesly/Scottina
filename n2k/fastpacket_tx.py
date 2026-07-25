"""NMEA2000 fast-packet TX framing — first outbound fast-packet in the
ecosystem (129029 GNSS Position Data needs it; Scottina Light will
want this again, so it is a standalone, socket-free module).

The framing mirrors what kilodash/n2k.py::FastPacketAssembler reassembles:

    frame 0:  [seq<<5 | 0] [total_len] [payload bytes 0..5]
    frame n:  [seq<<5 | n] [payload bytes 6+7(n-1) .. +7]

seq is a 3-bit counter (bits 5-7) that must CHANGE between consecutive
messages of the same (PGN, source) so a receiver can tell a restarted
sequence from a continuation; frames are always padded to 8 bytes with
0xFF. Max assembled payload 223 bytes (0..5 + 31*7).

The round-trip test (tests/test_n2knode.py) runs this splitter against
our own RX reassembler — the two independent implementations validate
each other, which also hardens the RX side with a second real producer.
"""

FP_MAX = 223
_FIRST_CHUNK = 6
_NEXT_CHUNK = 7


def split(payload, seq):
    """One assembled payload → the list of 8-byte fast-packet frames,
    tagged with the 3-bit sequence id `seq`."""
    if not 0 < len(payload) <= FP_MAX:
        raise ValueError(f"fast-packet payload must be 1..{FP_MAX} bytes, "
                         f"got {len(payload)}")
    seq = (seq & 0x7) << 5
    frames = [bytes([seq, len(payload)])
              + payload[:_FIRST_CHUNK].ljust(_FIRST_CHUNK, b"\xFF")]
    off = _FIRST_CHUNK
    idx = 1
    while off < len(payload):
        chunk = payload[off:off + _NEXT_CHUNK]
        frames.append(bytes([seq | idx]) + chunk.ljust(_NEXT_CHUNK, b"\xFF"))
        off += _NEXT_CHUNK
        idx += 1
    return frames


class FastPacketTx:
    """Per-PGN rotating sequence counter (the sender-side state)."""

    def __init__(self):
        self._seq = {}

    def frames(self, pgn, payload):
        seq = self._seq.get(pgn, -1) + 1 & 0x7
        self._seq[pgn] = seq
        return split(payload, seq)
