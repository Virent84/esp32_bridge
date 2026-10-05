#!/usr/bin/env python3
"""Smart Hearing Aid - 4-mic DOA + beamforming. Usage:
   python3 run.py                       # source from config.json (default: ESP32-S3 over USB), dashboard on :8080
   python3 run.py --esp32 /dev/ttyACM0  # force ESP32 source on a given port
   python3 run.py --sim                 # synthetic source, no hardware needed
   python3 run.py --wav in.wav          # replay a 4-channel recording (real-time paced, loops)
   python3 run.py --alsa                # legacy: 4 mics wired directly to the Pi (see legacy/)
"""
import argparse, logging, sys
from ha import config
from ha.audio_source import ALSASource, SimSource, WavSource, ESP32Source
from ha.pipeline import Pipeline
from ha.server import create_app

ap = argparse.ArgumentParser()
ap.add_argument("--config"); ap.add_argument("--sim", action="store_true"); ap.add_argument("--wav")
ap.add_argument("--esp32", nargs="?", const="auto", metavar="PORT"); ap.add_argument("--alsa", action="store_true")
ap.add_argument("--port", type=int, help="dashboard port"); ap.add_argument("--playback", action="store_true")
a = ap.parse_args()
cfg = config.load(a.config)
if a.playback: cfg["output"]["playback"] = True
if a.sim or cfg["source"] == "sim":   src = SimSource(cfg)
elif a.wav:                           src = WavSource(cfg, a.wav)
elif a.alsa or cfg["source"] == "alsa": src = ALSASource(cfg)
else:                                 src = ESP32Source(cfg, a.esp32)
pipe = Pipeline(cfg, src); pipe.start()
logging.getLogger("werkzeug").setLevel(logging.ERROR)
port = a.port or cfg["dashboard"]["port"]
print(f"Dashboard: http://<pi-ip>:{port}   (source: {src.name})", flush=True)
create_app(pipe).run(host=cfg["dashboard"]["host"], port=port, threaded=True)
