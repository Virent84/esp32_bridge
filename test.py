#!/usr/bin/env python3

"""
ESP32-S3 -> 4x INMP441 -> Native USB -> Raspberry Pi 5 test

ESP32 packet format, little endian:

    uint16 magic  = 0xA55A
    uint16 seq
    uint16 frames = 256
    uint16 flags
    int16  data[256][4]

Audio order:
    M1, M2, M3, M4

This program:
    1. Finds the ESP32-S3 USB serial device
    2. Opens it
    3. Synchronizes to packet magic 0xA55A
    4. Validates packet headers
    5. Checks sequence continuity
    6. Decodes all 4 microphones
    7. Calculates RMS / peak / dBFS
    8. Detects silent/disconnected microphones
    9. Reports USB throughput and packet rate
   10. Saves optional WAV output

Run:
    python3 test.py

Optional:
    python3 test.py --device /dev/ttyACM0
    python3 test.py --wav test_4mic.wav
"""

import argparse
import glob
import os
import struct
import sys
import time
import math

try:
    import serial
except ImportError:
    print("ERROR: pyserial is not installed.")
    print()
    print("Install it with:")
    print("    sudo apt update")
    print("    sudo apt install python3-serial")
    print()
    print("or:")
    print("    pip3 install pyserial")
    sys.exit(1)

# ============================================================
# ESP32 PACKET FORMAT
# ============================================================

MAGIC = 0xA55A
FRAMES = 256
CHANNELS = 4

HEADER_SIZE = 8
SAMPLE_SIZE = 2                 # int16
PAYLOAD_SIZE = FRAMES * CHANNELS * SAMPLE_SIZE
PACKET_SIZE = HEADER_SIZE + PAYLOAD_SIZE

# 8 + 256*4*2 = 2056 bytes
assert PACKET_SIZE == 2056

FS = 16000

# ============================================================
# DEVICE DISCOVERY
# ============================================================

def find_usb_devices():

    candidates = []

    patterns = [
        "/dev/ttyACM*",
        "/dev/ttyUSB*",
    ]

    for pattern in patterns:
        candidates.extend(glob.glob(pattern))

    return sorted(set(candidates))


def show_devices():

    devices = find_usb_devices()

    print("USB serial devices detected:")

    if not devices:
        print("  NONE")
        print()
        print("Check:")
        print("  1. ESP32-S3 is powered")
        print("  2. USB cable is connected")
        print("  3. Cable is a DATA cable")
        print("  4. Cable is connected to the ESP32 port labelled USB")
        return []

    for d in devices:
        print(f"  {d}")

    return devices


# ============================================================
# PACKET SYNCHRONIZATION
# ============================================================

def find_magic(ser):

    magic_bytes = struct.pack("<H", MAGIC)

    state = 0

    while True:

        b = ser.read(1)

        if not b:
            return False

        if state == 0:

            if b == magic_bytes[0:1]:
                state = 1

        else:

            if b == magic_bytes[1:2]:
                return True

            if b == magic_bytes[0:1]:
                state = 1
            else:
                state = 0


# ============================================================
# READ EXACT NUMBER OF BYTES
# ============================================================

def read_exact(ser, size):

    data = bytearray()

    while len(data) < size:

        chunk = ser.read(size - len(data))

        if not chunk:
            return None

        data.extend(chunk)

    return bytes(data)


# ============================================================
# AUDIO STATISTICS
# ============================================================

class ChannelStats:

    def __init__(self):

        self.sum_sq = 0.0
        self.peak = 0
        self.samples = 0

    def add(self, samples):

        for v in samples:

            av = abs(v)

            if av > self.peak:
                self.peak = av

            self.sum_sq += float(v) * float(v)
            self.samples += 1

    def rms(self):

        if self.samples == 0:
            return 0.0

        return math.sqrt(self.sum_sq / self.samples)

    def dbfs(self):

        r = self.rms()

        if r <= 0:
            return -120.0

        return 20.0 * math.log10(r / 32768.0)

    def peak_dbfs(self):

        if self.peak <= 0:
            return -120.0

        return 20.0 * math.log10(self.peak / 32768.0)


# ============================================================
# WAV WRITER
# ============================================================

class WavWriter:

    def __init__(self, filename, channels=4, sample_rate=16000):

        self.filename = filename
        self.channels = channels
        self.sample_rate = sample_rate
        self.frames = 0

        self.f = open(filename, "wb")

        # Placeholder WAV header
        self.f.write(b"RIFF")
        self.f.write(struct.pack("<I", 0))
        self.f.write(b"WAVE")

        self.f.write(b"fmt ")
        self.f.write(struct.pack("<I", 16))
        self.f.write(struct.pack(
            "<HHIIHH",
            1,                      # PCM
            channels,
            sample_rate,
            sample_rate * channels * 2,
            channels * 2,
            16
        ))

        self.f.write(b"data")
        self.f.write(struct.pack("<I", 0))

    def write(self, data):

        self.f.write(data)
        self.frames += len(data) // (self.channels * 2)

    def close(self):

        data_size = self.frames * self.channels * 2
        riff_size = 36 + data_size

        self.f.seek(4)
        self.f.write(struct.pack("<I", riff_size))

        self.f.seek(40)
        self.f.write(struct.pack("<I", data_size))

        self.f.close()


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="Test ESP32-S3 4-channel INMP441 USB audio stream"
    )

    parser.add_argument(
        "--device",
        help="Serial device, e.g. /dev/ttyACM0"
    )

    parser.add_argument(
        "--wav",
        help="Optional output WAV file"
    )

    parser.add_argument(
        "--seconds",
        type=float,
        default=0,
        help="Run for N seconds. 0 = until Ctrl+C"
    )

    args = parser.parse_args()

    print()
    print("=" * 70)
    print(" ESP32-S3 4-MIC USB AUDIO TEST")
    print("=" * 70)
    print()

    print(f"Expected packet size : {PACKET_SIZE} bytes")
    print(f"Frames per packet    : {FRAMES}")
    print(f"Channels             : {CHANNELS}")
    print(f"Sample rate          : {FS} Hz")
    print(f"Audio format         : signed 16-bit little endian")
    print(f"Channel order        : M1 M2 M3 M4")
    print()

    # --------------------------------------------------------
    # DEVICE
    # --------------------------------------------------------

    devices = show_devices()

    if args.device:

        device = args.device

    else:

        if not devices:

            sys.exit(1)

        # Prefer ACM because native USB CDC normally appears here
        acm = [d for d in devices if "ttyACM" in d]

        if acm:
            device = acm[0]
        else:
            device = devices[0]

    print()
    print(f"Using device: {device}")

    if not os.path.exists(device):

        print(f"ERROR: {device} does not exist.")
        sys.exit(1)

    # --------------------------------------------------------
    # OPEN SERIAL
    # --------------------------------------------------------

    print("Opening USB CDC...")

    try:

        ser = serial.Serial(
            port=device,
            baudrate=2000000,
            timeout=1,
            write_timeout=1
        )

    except Exception as e:

        print()
        print("ERROR opening device:")
        print(e)
        print()
        print("Try:")
        print(f"    ls -l {device}")
        print()
        print("If permission is denied:")
        print("    sudo usermod -aG dialout $USER")
        print("    reboot")

        sys.exit(1)

    print("USB CDC opened successfully.")
    print()

    # --------------------------------------------------------
    # IMPORTANT:
    # Do NOT reset/flush the ESP32 stream aggressively here.
    # We synchronize using the packet magic instead.
    # --------------------------------------------------------

    print("Waiting for ESP32 packet stream...")
    print("Searching for magic: 0xA55A")
    print()

    # --------------------------------------------------------
    # WAV
    # --------------------------------------------------------

    wav = None

    if args.wav:

        try:

            wav = WavWriter(args.wav)

            print(f"WAV recording: {args.wav}")

        except Exception as e:

            print(f"WARNING: Could not create WAV: {e}")
            wav = None

    # --------------------------------------------------------
    # STATISTICS
    # --------------------------------------------------------

    stats = [
        ChannelStats(),
        ChannelStats(),
        ChannelStats(),
        ChannelStats(),
    ]

    packet_count = 0
    valid_packets = 0
    bad_packets = 0
    restart_flags = 0
    sequence_errors = 0

    first_seq = None
    previous_seq = None

    total_bytes = 0
    total_frames = 0

    start_time = time.monotonic()
    last_report = start_time

    print("Listening...")
    print()

    try:

        while True:

            # ------------------------------------------------
            # Find packet magic
            # ------------------------------------------------

            found = find_magic(ser)

            if not found:
                print("USB timeout while searching for packet.")
                continue

            # ------------------------------------------------
            # Read remaining header
            #
            # We already consumed 2 bytes:
            #   magic
            #
            # Need:
            #   seq
            #   frames
            #   flags
            # ------------------------------------------------

            header_rest = read_exact(ser, 6)

            if header_rest is None:

                print("Incomplete packet header.")
                bad_packets += 1
                continue

            seq, frames, flags = struct.unpack(
                "<HHH",
                header_rest
            )

            # ------------------------------------------------
            # Validate header
            # ------------------------------------------------

            if frames != FRAMES:

                print(
                    f"BAD HEADER: seq={seq}, "
                    f"frames={frames}, expected={FRAMES}"
                )

                bad_packets += 1

                continue

            # ------------------------------------------------
            # Read audio payload
            # ------------------------------------------------

            payload = read_exact(ser, PAYLOAD_SIZE)

            if payload is None:

                print(
                    f"INCOMPLETE PAYLOAD: seq={seq}"
                )

                bad_packets += 1

                continue

            # ------------------------------------------------
            # Sequence check
            # ------------------------------------------------

            if first_seq is None:

                first_seq = seq

            if previous_seq is not None:

                expected = (previous_seq + 1) & 0xFFFF

                if seq != expected:

                    sequence_errors += 1

                    print(
                        f"SEQUENCE GAP: "
                        f"expected={expected}, got={seq}"
                    )

            previous_seq = seq

            # ------------------------------------------------
            # Restart flag
            # ------------------------------------------------

            if flags & 1:

                restart_flags += 1

                print(
                    f"WARNING: ESP32 I2S restart flag "
                    f"on packet seq={seq}"
                )

            # ------------------------------------------------
            # Decode audio
            #
            # payload is:
            #
            # frame 0: M1 M2 M3 M4
            # frame 1: M1 M2 M3 M4
            # ...
            # ------------------------------------------------

            samples = struct.unpack(
                "<{}h".format(FRAMES * CHANNELS),
                payload
            )

            # ------------------------------------------------
            # Per-channel statistics
            # ------------------------------------------------

            for ch in range(CHANNELS):

                channel_samples = samples[ch::CHANNELS]

                stats[ch].add(channel_samples)

            # ------------------------------------------------
            # Save WAV
            # ------------------------------------------------

            if wav:

                wav.write(payload)

            packet_count += 1
            valid_packets += 1

            total_bytes += HEADER_SIZE + PAYLOAD_SIZE
            total_frames += FRAMES

            # ------------------------------------------------
            # Periodic report
            # ------------------------------------------------

            now = time.monotonic()

            if now - last_report >= 1.0:

                elapsed = now - start_time

                pps = packet_count / elapsed
                fps = total_frames / elapsed
                kbps = (total_bytes * 8 / elapsed) / 1000.0

                print()
                print(
                    f"Packets={packet_count:6d} | "
                    f"Packets/s={pps:7.2f} | "
                    f"Frames/s={fps:8.1f} | "
                    f"USB={kbps:7.1f} kbps"
                )

                print(
                    f"Seq errors={sequence_errors} | "
                    f"I2S restarts={restart_flags} | "
                    f"Bad packets={bad_packets}"
                )

                print(
                    "--------------------------------------------------------------"
                )

                for ch in range(CHANNELS):

                    rms = stats[ch].rms()
                    dbfs = stats[ch].dbfs()
                    peak = stats[ch].peak
                    peak_dbfs = stats[ch].peak_dbfs()

                    print(
                        f"M{ch+1}: "
                        f"RMS={rms:8.1f} "
                        f"dBFS={dbfs:7.2f} "
                        f"Peak={peak:6d} "
                        f"Peak_dBFS={peak_dbfs:7.2f}"
                    )

                print(
                    "--------------------------------------------------------------"
                )

                # ------------------------------------------------
                # Simple diagnostics
                # ------------------------------------------------

                print("DIAGNOSTIC:")

                for ch in range(CHANNELS):

                    db = stats[ch].dbfs()

                    if db < -90:

                        print(
                            f"  M{ch+1}: VERY LOW / POSSIBLY DISCONNECTED"
                        )

                    elif db < -60:

                        print(
                            f"  M{ch+1}: low signal"
                        )

                    else:

                        print(
                            f"  M{ch+1}: signal detected"
                        )

                if pps < 55:

                    print(
                        "  WARNING: packet rate is much lower than "
                        "expected ~62.5 packets/s"
                    )

                if sequence_errors > 0:

                    print(
                        "  WARNING: packet loss/discontinuity detected"
                    )

                if restart_flags > 0:

                    print(
                        "  WARNING: ESP32 I2S has restarted"
                    )

                print()

                last_report = now

            # ------------------------------------------------
            # Time limit
            # ------------------------------------------------

            if args.seconds > 0:

                if now - start_time >= args.seconds:

                    break

    except KeyboardInterrupt:

        print()
        print("Stopping...")

    finally:

        if wav:

            wav.close()

            print(
                f"WAV saved: {args.wav}"
            )

        ser.close()

    # ========================================================
    # FINAL REPORT
    # ========================================================

    elapsed = time.monotonic() - start_time

    print()
    print("=" * 70)
    print(" FINAL TEST RESULT")
    print("=" * 70)

    print()
    print(f"Device              : {device}")
    print(f"Test duration       : {elapsed:.2f} s")
    print(f"Valid packets       : {valid_packets}")
    print(f"Bad packets         : {bad_packets}")
    print(f"Sequence errors     : {sequence_errors}")
    print(f"I2S restart flags   : {restart_flags}")
    print(f"Audio frames        : {total_frames}")

    if elapsed > 0:

        print(
            f"Packet rate         : "
            f"{valid_packets / elapsed:.2f} packets/s"
        )

        print(
            f"Audio frame rate    : "
            f"{total_frames / elapsed:.1f} frames/s"
        )

        print(
            f"USB data rate       : "
            f"{(total_bytes * 8 / elapsed) / 1000:.1f} kbps"
        )

    print()
    print("CHANNEL RESULTS")
    print("-" * 70)

    for ch in range(CHANNELS):

        print(
            f"M{ch+1}: "
            f"RMS={stats[ch].rms():8.1f} | "
            f"dBFS={stats[ch].dbfs():7.2f} | "
            f"Peak={stats[ch].peak:6d} | "
            f"Peak dBFS={stats[ch].peak_dbfs():7.2f}"
        )

    print()
    print("VERDICT")
    print("-" * 70)

    problems = []

    if valid_packets == 0:
        problems.append("No valid audio packets received")

    if sequence_errors > 0:
        problems.append("USB packet sequence gaps detected")

    if restart_flags > 0:
        problems.append("ESP32 reported I2S restarts")

    for ch in range(CHANNELS):

        if stats[ch].samples == 0:
            problems.append(f"M{ch+1}: no samples")

        elif stats[ch].dbfs() < -90:
            problems.append(
                f"M{ch+1}: extremely low signal"
            )

    if not problems:

        print("PASS")
        print()
        print("ESP32-S3 -> USB -> Raspberry Pi 5 audio stream")
        print("is being received correctly.")
        print()
        print("All four microphone channels are producing data.")

    else:

        print("FAIL / WARNING")
        print()

        for p in problems:
            print(" - " + p)

    print()
    print("=" * 70)


if __name__ == "__main__":
    main()
