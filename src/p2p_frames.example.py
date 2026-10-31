"""Example of the file tools/extract_p2p_frames.py produces.

Copy it to `p2p_frames.py` and replace the values with YOUR OWN capture.
Everything below is a placeholder, not real data.

    python tools/extract_p2p_frames.py talk.pcap > p2p_frames.py

`p2p_frames.py` is listed in .gitignore -- never commit your capture.
"""

# Ordered phone -> camera DRW frames from your own pcap, as hex strings.
CAPTURED_CONTROL = [
    # '....',
]

# The login request (opcode 4096) as a hex string.
CAPTURED_LOGIN = ''

# START_TALK (opcode 4113) and STOP_TALK (opcode 4114) as raw bytes.
CAPTURED_START_TALK = b''
CAPTURED_STOP_TALK = b''
