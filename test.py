import sys, glob, time, wave, serial
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10
out  = sys.argv[2] if len(sys.argv) > 2 else "mics4ch.wav"
port = sorted(glob.glob("/dev/ttyACM*"))[0]
ser = serial.Serial(port, 2000000, timeout=1)
ser.write(b"x"); time.sleep(0.3); ser.reset_input_buffer()
ser.write(b"s")
need = int(secs * 16000) * 8                      # 4 ch x 2 bytes per frame
data = bytearray()
while len(data) < need:
    d = ser.read(min(65536, need - len(data)))
    if not d: sys.exit("no data - check cable/port")
    data += d
ser.write(b"x")
with wave.open(out, "wb") as w:
    w.setnchannels(4); w.setsampwidth(2); w.setframerate(16000); w.writeframes(bytes(data))
print("wrote", out, len(data) // 8, "frames")
