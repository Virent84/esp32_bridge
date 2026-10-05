#!/usr/bin/env python3
"""Record the 4 ESP32 microphones to a 4-channel 16-bit WAV.

ESP32-S3 protocol:
  - native USB CDC (/dev/ttyACM*)
  - send 's' to start
  - raw interleaved int16 little-endian PCM: M1,M2,M3,M4
  - 16 kHz, 4 channels
  - send 'x' to stop

Usage:
  python3 tools/esp32_record.py --seconds 10 --out data/mics4ch.wav
  python3 tools/esp32_record.py --port /dev/ttyACM0 --seconds 10 --out data/mics4ch.wav
"""

import argparse
import glob
import os
import sys
import time
import wave

import numpy as np


def find_port():
    """Prefer the Espressif native USB CDC device."""
    try:
        from serial.tools import list_ports

        for p in list_ports.comports():
            if p.vid == 0x303A:
                return p.device
    except Exception:
        pass

    ports = sorted(glob.glob("/dev/ttyACM*"))
    return ports[0] if ports else None


class ESP32RawSource:
    """Raw PCM reader for the ESP32-S3 firmware."""

    def __init__(self, fs=16000, port=None, baud=115200):
        try:
            import serial
        except ImportError:
            raise RuntimeError(
                "pyserial is not installed.\n"
                "Install it with:\n"
                "  python3 -m pip install pyserial"
            )

        self.serial = serial
        self.fs = int(fs)
        self.port = port or find_port()
        self.baud = int(baud)

        self.channels = 4
        self.bytes_per_sample = 2
        self.bytes_per_frame = self.channels * self.bytes_per_sample

        self.ser = None
        self.started = False

        if not self.port:
            raise RuntimeError(
                "No ESP32 USB serial device found.\n"
                "Check:\n"
                "  ls /dev/ttyACM*\n"
                "  lsusb | grep 303a"
            )

        print(f"Opening ESP32 on {self.port} ...")

        try:
            self.ser = serial.Serial(
                self.port,
                self.baud,
                timeout=0.25,
                write_timeout=1.0,
                exclusive=True,
            )
        except TypeError:
            # Compatibility with older pyserial versions.
            self.ser = serial.Serial(
                self.port,
                self.baud,
                timeout=0.25,
                write_timeout=1.0,
            )
        except serial.SerialException as e:
            raise RuntimeError(
                f"Cannot open {self.port}: {e}"
            ) from e

        # Native USB CDC can reset the ESP32 when the port is opened.
        # Give the board time to reboot and enumerate.
        time.sleep(1.5)

        # We don't want any boot/status bytes in the PCM stream.
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

        # Force a known stopped state.
        self.ser.write(b"x")
        self.ser.flush()

        time.sleep(0.05)

        self.ser.reset_input_buffer()

        # Start raw PCM streaming.
        print("Sending START...")
        self.ser.write(b"s")
        self.ser.flush()

        self.started = True

    def read(self, n):
        """Read n audio frames and return float32 [4,n]."""

        n = int(n)
        required = n * self.bytes_per_frame

        data = bytearray()

        # Expected time + generous margin.
        deadline = (
            time.monotonic()
            + max(2.0, n / self.fs + 1.5)
        )

        while len(data) < required:

            if time.monotonic() > deadline:
                raise RuntimeError(
                    "ESP32 audio timeout.\n"
                    f"Received {len(data)}/{required} bytes.\n"
                    "The USB device opened successfully, but the ESP32 "
                    "did not produce the expected PCM stream.\n"
                    "Check the Arduino firmware/I2S capture path."
                )

            chunk = self.ser.read(required - len(data))

            if chunk:
                data.extend(chunk)

        raw = np.frombuffer(
            data,
            dtype="<i2"
        )

        expected_samples = n * self.channels

        if raw.size != expected_samples:
            raise RuntimeError(
                f"Incomplete PCM block: "
                f"{raw.size}/{expected_samples} samples"
            )

        # Interleaved:
        #
        # M1 M2 M3 M4
        # M1 M2 M3 M4
        #
        # Convert to:
        #
        # [M1]
        # [M2]
        # [M3]
        # [M4]

        x = raw.reshape(
            n,
            self.channels
        ).T.astype(np.float32)

        return x / 32768.0

    def close(self):
        if self.ser is None:
            return

        try:
            if self.started:
                self.ser.write(b"x")
                self.ser.flush()
                time.sleep(0.05)
        except Exception:
            pass

        try:
            self.ser.close()
        except Exception:
            pass

        self.ser = None
        self.started = False


def main():

    ap = argparse.ArgumentParser()

    ap.add_argument("--port")

    ap.add_argument(
        "--seconds",
        type=float,
        default=10
    )

    ap.add_argument(
        "--out",
        default="data/mics4ch.wav"
    )

    ap.add_argument("--config")

    a = ap.parse_args()

    # Keep existing project configuration behavior.
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(__file__),
            ".."
        )
    )

    try:
        from ha import config

        cfg = config.load(a.config)

        fs = int(
            cfg["sample_rate"]
        )

        esp_cfg = cfg.get(
            "esp32",
            {}
        )

        baud = int(
            esp_cfg.get(
                "baud",
                115200
            )
        )

    except Exception:

        # Fallback for a standalone recorder.
        fs = 16000
        baud = 115200

    if fs != 16000:
        raise SystemExit(
            "ERROR: The ESP32 firmware is fixed at "
            f"16 kHz, but config.json specifies {fs} Hz."
        )

    if a.seconds <= 0:
        raise SystemExit(
            "ERROR: --seconds must be greater than zero."
        )

    src = None

    try:

        src = ESP32RawSource(
            fs=fs,
            port=a.port,
            baud=baud
        )

        print(
            f"ESP32 {src.port} connected"
        )

        print(
            "Format: 16 kHz / 4 channels / int16"
        )

        print(
            f"Recording {a.seconds:.1f} s ... speak now"
        )

        total_frames = int(
            round(
                a.seconds * fs
            )
        )

        block = 1024

        captured = 0

        chunks = []

        while captured < total_frames:

            take = min(
                block,
                total_frames - captured
            )

            x = src.read(take)

            chunks.append(x)

            captured += take

            print(
                f"\rCaptured "
                f"{captured}/{total_frames} frames",
                end="",
                flush=True
            )

        print()

        x = np.concatenate(
            chunks,
            axis=1
        )

        x = np.clip(
            x,
            -1.0,
            1.0
        )

        os.makedirs(
            os.path.dirname(a.out) or ".",
            exist_ok=True
        )

        with wave.open(
            a.out,
            "wb"
        ) as w:

            w.setnchannels(4)

            w.setsampwidth(2)

            w.setframerate(fs)

            pcm16 = (
                x * 32767.0
            ).round().astype("<i2")

            w.writeframes(
                pcm16.T.copy().tobytes()
            )

        rms = 20 * np.log10(
            np.sqrt(
                np.mean(
                    x ** 2,
                    axis=1
                )
            )
            + 1e-12
        )

        print(
            f"wrote {a.out}"
        )

        print(
            "levels dBFS: "
            + "  ".join(
                f"M{i + 1}:{rms[i]:.1f}"
                for i in range(4)
            )
        )

    except KeyboardInterrupt:

        print(
            "\nRecording stopped by user."
        )

        return 130

    except Exception as e:

        print(
            f"\nERROR: {e}",
            file=sys.stderr
        )

        return 1

    finally:

        if src is not None:
            src.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
