/*
 * ESP32-S3  ->  4x INMP441  ->  USB  ->  Raspberry Pi 5
 *
 * Build: Arduino-ESP32 core 3.x (ESP-IDF 5.x underneath)
 *   Board: ESP32S3 Dev Module
 *   USB CDC On Boot: Enabled
 *   Plug the cable into the port labelled "USB" (native USB), not "UART".
 *
 * Two I2S peripherals are used so that all 4 mics share ONE clock:
 *   I2S0 = MASTER, generates BCLK + WS, reads mics M1 (L) and M2 (R) on DIN_A
 *   I2S1 = SLAVE,  listens to the same BCLK + WS, reads mics M3 (L) and M4 (R) on DIN_B
 * The slave's clock pins are separate GPIOs jumpered to the master's clock pins.
 *
 * Mic positions (must match simulation/microphone_array.py):
 *   M1 = +x, M2 = +y, M3 = -x, M4 = -y   (5 cm radius circle)
 * INMP441 L/R pin: GND = left slot, 3V3 = right slot.
 *
 * Packet on USB (little endian):
 *   uint16 magic  = 0xA55A
 *   uint16 seq    (wraps)
 *   uint16 frames = 256
 *   uint16 flags  (bit0 = I2S was restarted before this packet, i.e. discontinuity)
 *   int16  data[256][4]   interleaved M1,M2,M3,M4
 */
#include <Arduino.h>
#include "driver/i2s_std.h"

// ---------------- pins (ESP32-S3 DevKitC-1 safe choices) ----------------
static const gpio_num_t PIN_BCLK_OUT = GPIO_NUM_4;   // master BCLK out
static const gpio_num_t PIN_WS_OUT   = GPIO_NUM_5;   // master WS out
static const gpio_num_t PIN_DIN_A    = GPIO_NUM_6;   // mics M1 + M2 data
static const gpio_num_t PIN_BCLK_IN  = GPIO_NUM_15;  // jumper wire to GPIO4
static const gpio_num_t PIN_WS_IN    = GPIO_NUM_16;  // jumper wire to GPIO5
static const gpio_num_t PIN_DIN_B    = GPIO_NUM_7;   // mics M3 + M4 data

// ---------------- audio settings ----------------
static const uint32_t FS            = 16000;  // pipeline requires exactly 16 kHz
static const size_t   FRAMES        = 256;    // one STFT hop per packet (16 ms)
static const int      SHIFT         = 14;     // int32 slot -> int16. 16 = unity, 14 = +12 dB
static const uint16_t MAGIC         = 0xA55A;

// Set to 1 to print per-mic levels as TEXT on the UART port (Serial0, 115200)
// once per second. Open the Serial Monitor on the "UART" USB port to read it.
// Audio packets still go out of the native "USB" port.
#define DEBUG_UART 1

static i2s_chan_handle_t rxA = nullptr;  // master
static i2s_chan_handle_t rxB = nullptr;  // slave

static int32_t rawA[FRAMES * 2];
static int32_t rawB[FRAMES * 2];
static uint8_t packet[8 + FRAMES * 4 * 2];

static uint16_t seqNo = 0;
static uint16_t flags = 0;

static inline int16_t to16(int32_t v) {
  int32_t s = v >> SHIFT;
  if (s > 32767) s = 32767;
  if (s < -32768) s = -32768;
  return (int16_t)s;
}

static void configChannel(i2s_chan_handle_t &h, i2s_port_t port, i2s_role_t role,
                          gpio_num_t bclk, gpio_num_t ws, gpio_num_t din) {
  i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(port, role);
  cc.dma_desc_num  = 8;
  cc.dma_frame_num = FRAMES;
  ESP_ERROR_CHECK(i2s_new_channel(&cc, nullptr, &h));

  i2s_std_config_t sc = {};
  sc.clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(FS);
  sc.slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO);
  sc.gpio_cfg.mclk = I2S_GPIO_UNUSED;
  sc.gpio_cfg.bclk = bclk;
  sc.gpio_cfg.ws   = ws;
  sc.gpio_cfg.dout = I2S_GPIO_UNUSED;
  sc.gpio_cfg.din  = din;
  sc.gpio_cfg.invert_flags.mclk_inv = false;
  sc.gpio_cfg.invert_flags.bclk_inv = false;
  sc.gpio_cfg.invert_flags.ws_inv   = false;
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(h, &sc));
}

// Slave first, then master, so both start on the very first WS edge.
static void startI2S() {
  ESP_ERROR_CHECK(i2s_channel_enable(rxB));
  ESP_ERROR_CHECK(i2s_channel_enable(rxA));
}

static void restartI2S() {
  i2s_channel_disable(rxA);
  i2s_channel_disable(rxB);
  startI2S();
  flags |= 1;
}

#if DEBUG_UART
static double   accSq[4] = {0, 0, 0, 0};
static int16_t  accPk[4] = {0, 0, 0, 0};
static uint32_t accN = 0, pktCount = 0, restartCount = 0, lastPrint = 0;
#endif

void setup() {
#if DEBUG_UART
  Serial0.begin(115200);           // UART0 (TX=GPIO43, RX=GPIO44) -> "UART" USB port
  delay(300);
  Serial0.println("\n[mic bridge] booting");
#endif
  Serial.setTxBufferSize(16384);   // must be before begin()
  Serial.begin(2000000);           // baud is ignored on native USB CDC
  Serial.setTxTimeoutMs(5);        // never stall the I2S loop if no host is reading
  delay(500);

  configChannel(rxA, I2S_NUM_0, I2S_ROLE_MASTER, PIN_BCLK_OUT, PIN_WS_OUT, PIN_DIN_A);
  configChannel(rxB, I2S_NUM_1, I2S_ROLE_SLAVE,  PIN_BCLK_IN,  PIN_WS_IN,  PIN_DIN_B);
  startI2S();
#if DEBUG_UART
  Serial0.println("[mic bridge] I2S0 master + I2S1 slave started, streaming on native USB");
#endif

  // fixed header fields
  memcpy(packet + 0, &MAGIC, 2);
  uint16_t fr = FRAMES;
  memcpy(packet + 4, &fr, 2);
}

void loop() {
  static uint32_t last = micros();
  size_t n = 0;

  if (i2s_channel_read(rxA, rawA, sizeof(rawA), &n, 100) != ESP_OK || n != sizeof(rawA)) { restartI2S(); return; }
  if (i2s_channel_read(rxB, rawB, sizeof(rawB), &n, 100) != ESP_OK || n != sizeof(rawB)) { restartI2S(); return; }

  // If the loop was stalled long enough for the DMA rings to overflow, A and B
  // may no longer be aligned. Restart both and flag the discontinuity.
  uint32_t now = micros();
  if (now - last > 60000) { last = now; restartI2S(); return; }
  last = now;

  int16_t *out = (int16_t *)(packet + 8);
  for (size_t i = 0; i < FRAMES; i++) {
    out[i * 4 + 0] = to16(rawA[i * 2 + 0]);  // M1 (+x)
    out[i * 4 + 1] = to16(rawA[i * 2 + 1]);  // M2 (+y)
    out[i * 4 + 2] = to16(rawB[i * 2 + 0]);  // M3 (-x)
    out[i * 4 + 3] = to16(rawB[i * 2 + 1]);  // M4 (-y)
  }
#if DEBUG_UART
  for (size_t i = 0; i < FRAMES; i++)
    for (int c = 0; c < 4; c++) {
      int16_t v = out[i * 4 + c];
      accSq[c] += (double)v * v;
      int16_t a = v < 0 ? (v == -32768 ? 32767 : -v) : v;
      if (a > accPk[c]) accPk[c] = a;
    }
  accN += FRAMES; pktCount++;
  if (flags & 1) restartCount++;
  if (millis() - lastPrint >= 1000) {
    lastPrint = millis();
    Serial0.printf("pkts/s=%lu restarts=%lu |", (unsigned long)pktCount, (unsigned long)restartCount);
    for (int c = 0; c < 4; c++) {
      double rms = sqrt(accSq[c] / (double)accN) / 32768.0;
      Serial0.printf(" M%d rms=%6.1fdBFS pk=%5d", c + 1, 20.0 * log10(rms + 1e-9), accPk[c]);
      accSq[c] = 0; accPk[c] = 0;
    }
    Serial0.println();
    accN = 0; pktCount = 0;
  }
#endif
  memcpy(packet + 2, &seqNo, 2);
  memcpy(packet + 6, &flags, 2);
  seqNo++;
  flags = 0;

  Serial.write(packet, sizeof(packet));
}
