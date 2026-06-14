# main.py
# ESP-WROOM-32 MicroPython
# Bosch BMP280 (pressure) + IST AG HYT (temperature/humidity) over I2C
# -> Volkszaehler
#
# Robustness patterns ported from the HMP233 node, which earned them in the
# field: guarded startup that always reaches the main loop, a Wi-Fi connect
# with a real timeout (no infinite busy-wait), a hardware watchdog, ONE
# persistent keep-alive HTTP connection instead of a fresh socket per POST
# (the fresh-socket pattern fragments MicroPython's non-compacting heap and
# causes silent multi-hour upload gaps), per-value bound-checked posts, and
# persistent reset/error logs so failures leave a trace across reboots.

import machine
import network
import time
import ntptime
import gc
import socket
import os

from bmp280 import *
from config import WIFI_SSID, WIFI_PASSWORD

# ----------------------------
# Config
# ----------------------------
HOSTNAME = "pressure-sensor"

# I2C bus
I2C_ID = 0
I2C_SCL = 22
I2C_SDA = 21
HYT_ADDR = 0x28          # IST AG HYT humidity/temperature sensor
HYT_MEASURE_DELAY_S = 0.1  # HYT needs ~50-60 ms after the measure request

# Runtime
LOOP_PERIOD_S = 60
RUNTIME_HOURS = 6

# Safety net: if a post hits MemoryError on this many *consecutive* loop
# iterations, reboot. A reboot is the only thing that fully defragments the
# heap (MicroPython's GC is non-compacting, so gc.collect() alone can't free a
# contiguous block for the socket once the heap is shredded). The persistent
# VzClient connection below should make this almost never fire.
MAX_CONSECUTIVE_MEM_ERRORS = 5

# Watchdog: must be longer than one worst-case loop iteration
# (wifi reconnect timeout + sensor read + 2x HTTP post timeout + sleep).
WDT_TIMEOUT_MS = 180000

# HTTP POST timeout, so a slow/unreachable middleware can't block the loop.
HTTP_TIMEOUT_S = 10

RESET_LOG_FILE = "reset_log.txt"

# Persistent log of caught loop/wifi/http exceptions (not sent to Volkszaehler).
# Size-capped: once it grows past MAX_ERR_LOG_BYTES the file is dropped and
# restarted, so a failure storm can't fill up the flash.
LOOP_ERR_LOG_FILE = "loop_err_log.txt"
MAX_ERR_LOG_BYTES = 20000

# Volkszaehler: the middleware host is resolved by name (DHCP-safe), and all
# channels share the same host -- only the UUID path differs.
VZ_HOST = "volkszaehler-in"
VZ_PORT = 80
VZ_TEMP_PATH = "/middleware/data/3bcaceb0-6543-11ee-8290-9fb1c7c0202b.json"
VZ_PRESS_PATH = "/middleware/data/df0f3640-6543-11ee-84f3-e9c4bed13213.json"
VZ_RH_PATH = "/middleware/data/b628e700-6543-11ee-8365-e1ea2c002e32.json"

# Humidity was historically read but never uploaded. Set True to enable the
# RH channel (VZ_RH_PATH) as well.
POST_HUMIDITY = False

# Plausibility ranges -- a bad I2C read or noisy value is dropped, not posted.
TEMP_MIN, TEMP_MAX = -50.0, 100.0      # deg C
RH_MIN, RH_MAX = 0.0, 100.0            # % RH
PRESS_MIN, PRESS_MAX = 300.0, 1100.0   # hPa


# ----------------------------
# Helpers
# ----------------------------
def log_exc(prefix, e):
    line = "{}  {} {}".format(time.localtime(), prefix, repr(e))
    print(line)
    try:
        try:
            if os.stat(LOOP_ERR_LOG_FILE)[6] > MAX_ERR_LOG_BYTES:
                os.remove(LOOP_ERR_LOG_FILE)
        except OSError:
            pass
        with open(LOOP_ERR_LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


_RESET_CAUSE_NAMES = {
    machine.PWRON_RESET: "PWRON_RESET",
    machine.HARD_RESET: "HARD_RESET",
    machine.WDT_RESET: "WDT_RESET",
    machine.DEEPSLEEP_RESET: "DEEPSLEEP_RESET",
    machine.SOFT_RESET: "SOFT_RESET",
}


def log_reset_cause():
    """Append the reason for the last reset to a local file, so unplanned
    hard/brownout/WDT resets can be told apart from the deliberate 6h
    machine.reset() restarts."""
    try:
        cause = machine.reset_cause()
        name = _RESET_CAUSE_NAMES.get(cause, "UNKNOWN")
        line = "{}  cause={} ({})\n".format(time.localtime(), cause, name)
        with open(RESET_LOG_FILE, "a") as f:
            f.write(line)
        print("Reset cause:", name, "-> appended to", RESET_LOG_FILE)
    except Exception as e:
        log_exc("reset log failed:", e)


def wifi_connect(timeout_s=20):
    sta = network.WLAN(network.STA_IF)
    sta.active(True)

    try:
        network.hostname(HOSTNAME)
    except Exception:
        pass

    if sta.isconnected():
        return

    print("Connecting Wi-Fi...")
    sta.connect(WIFI_SSID, WIFI_PASSWORD)

    t0 = time.ticks_ms()
    while not sta.isconnected():
        if time.ticks_diff(time.ticks_ms(), t0) > timeout_s * 1000:
            raise OSError("Wi-Fi connect timeout")
        time.sleep_ms(200)

    print("Wi-Fi OK:", sta.ifconfig())


def ensure_wifi():
    if not network.WLAN(network.STA_IF).isconnected():
        wifi_connect()


def ntp_sync_once():
    try:
        ntptime.settime()
        print("NTP synced:", time.localtime())
    except Exception as e:
        log_exc("NTP failed:", e)


class VzClient:
    """Persistent HTTP/1.1 keep-alive connection to the Volkszaehler middleware.

    Opening a *fresh* socket for every POST (as urequests does) needs a
    contiguous lwIP buffer each time, and after enough create/destroy cycles
    the non-compacting heap is fragmented enough that no contiguous block
    exists -> MemoryError on every subsequent post for the rest of the run
    (silent multi-hour data gaps). gc.collect() can't help.

    Reusing ONE connection across posts and loop iterations removes nearly all
    of that socket churn. The connection is re-established transparently if the
    server closes an idle keep-alive socket or anything else goes wrong, and
    the host is re-resolved on reconnect so a changed DHCP lease is picked up.
    """

    def __init__(self, host, port=80, timeout_s=HTTP_TIMEOUT_S):
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.sock = None
        self.addr = None

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _connect(self):
        self.close()
        gc.collect()
        if self.addr is None:
            self.addr = socket.getaddrinfo(self.host, self.port)[0][-1]
        s = socket.socket()
        s.settimeout(self.timeout_s)
        s.connect(self.addr)
        self.sock = s

    def _read_response(self):
        """Consume exactly one HTTP response so the socket stays usable for the
        next keep-alive request. Closes the connection if the server doesn't
        give us a Content-Length to frame on (then the next post reconnects)."""
        s = self.sock
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(256)
            if not chunk:
                raise OSError("VZ closed connection before response headers")
            buf += chunk
            if len(buf) > 4096:
                raise OSError("VZ response headers too large")

        header_blob, _, body = buf.partition(b"\r\n\r\n")
        content_length = None
        keep_alive = True  # HTTP/1.1 default
        for h in header_blob.split(b"\r\n")[1:]:
            hl = h.lower()
            if hl.startswith(b"content-length:"):
                try:
                    content_length = int(h.split(b":", 1)[1].strip())
                except Exception:
                    content_length = None
            elif hl.startswith(b"connection:") and b"close" in hl:
                keep_alive = False

        if content_length is None:
            # Can't safely frame -> drain until close and don't reuse.
            keep_alive = False
            while s.recv(512):
                pass
        else:
            need = content_length - len(body)
            while need > 0:
                chunk = s.recv(need if need < 512 else 512)
                if not chunk:
                    break
                need -= len(chunk)

        if not keep_alive:
            self.close()

    def post(self, path, value):
        """POST one value; reuse the live connection, reconnecting once if the
        keep-alive socket has gone stale."""
        full_path = "{}?operation=add&value={}".format(path, value)
        req = (
            "POST {} HTTP/1.1\r\n"
            "Host: {}\r\n"
            "Connection: keep-alive\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        ).format(full_path, self.host).encode("ascii")

        for attempt in (1, 2):
            try:
                if self.sock is None:
                    self._connect()
                self.sock.send(req)
                self._read_response()
                return
            except Exception:
                self.close()
                # Force a fresh DNS lookup on the retry so a changed server IP
                # recovers without waiting for the next reboot.
                self.addr = None
                if attempt == 2:
                    raise
                gc.collect()


# ----------------------------
# Sensors (I2C)
# ----------------------------
def make_i2c():
    return machine.I2C(
        I2C_ID,
        scl=machine.Pin(I2C_SCL),
        sda=machine.Pin(I2C_SDA),
    )


def init_bmp(i2c):
    """Construct and configure the BMP280. The constructor reads calibration
    over I2C and raises if the sensor isn't responding, so callers must guard
    this and retry."""
    bmp = BMP280(i2c)
    bmp.use_case(BMP280_CASE_WEATHER)
    bmp.oversample(BMP280_OS_HIGH)
    bmp.temp_os = BMP280_TEMP_OS_8
    bmp.press_os = BMP280_PRES_OS_4
    bmp.standby = BMP280_STANDBY_250
    bmp.iir = BMP280_IIR_FILTER_2
    bmp.power_mode = BMP280_POWER_FORCED
    return bmp


def read_hyt(i2c):
    """Read humidity (%RH) and temperature (deg C) from the IST AG HYT sensor.
    Returns (rh, t). Raises on I2C error so the caller can skip this cycle."""
    i2c.writeto(HYT_ADDR, b"\x00")  # measurement request
    time.sleep(HYT_MEASURE_DELAY_S)
    reading = i2c.readfrom(HYT_ADDR, 4)
    # Top 2 bits of byte 0 are status; mask them off.
    humidity = ((reading[0] & 0x3F) * 0x100 + reading[1]) * (100.0 / 16383.0)
    # Bottom 2 bits of byte 3 are a dummy; shift them out.
    temperature = 165.0 / 16383.0 * ((reading[2] * 0x100 + (reading[3] & 0xFC)) >> 2) - 40
    return humidity, temperature


def read_pressure(bmp):
    """Force a single BMP280 measurement and return pressure in hPa."""
    bmp.force_measure()
    while bmp.is_measuring:
        time.sleep(0.1)
    while bmp.is_updating:
        time.sleep(0.1)
    p = bmp.pressure / 100
    bmp.sleep()
    return p


# ----------------------------
# Main
# ----------------------------
def main():
    log_reset_cause()

    try:
        wifi_connect()
    except Exception as e:
        log_exc("Wi-Fi startup failed:", e)

    ntp_sync_once()

    i2c = make_i2c()
    print("I2C devices:", [hex(d) for d in i2c.scan()])

    # Try a few times to bring up the BMP280, but never block boot forever: if
    # it stays unreachable we still run the loop (Wi-Fi heartbeat + humidity if
    # enabled) and retry the sensor each cycle rather than boot-looping.
    bmp = None
    for _ in range(3):
        try:
            bmp = init_bmp(i2c)
            print("BMP280 ready")
            break
        except Exception as e:
            log_exc("BMP280 init failed:", e)
            time.sleep(1)

    # Watchdog: if any single loop iteration (incl. wifi reconnect, sensor read
    # and HTTP posts) takes longer than WDT_TIMEOUT_MS without reaching
    # wdt.feed() again, the device resets itself instead of hanging forever.
    wdt = machine.WDT(timeout=WDT_TIMEOUT_MS)

    vz = VzClient(VZ_HOST, VZ_PORT)
    mem_err_count = 0

    start_ms = time.ticks_ms()
    runtime_ms = RUNTIME_HOURS * 3600 * 1000

    while time.ticks_diff(time.ticks_ms(), start_ms) < runtime_ms:
        wdt.feed()
        gc.collect()

        try:
            ensure_wifi()
        except Exception as e:
            log_exc("Wi-Fi reconnect:", e)

        try:
            # Temperature + humidity come from the HYT sensor.
            rh, t = read_hyt(i2c)

            if TEMP_MIN <= t <= TEMP_MAX:
                vz.post(VZ_TEMP_PATH, t)
                print("Sent T:", t)
            else:
                print("T ignored (out of range):", t)

            if POST_HUMIDITY:
                if RH_MIN <= rh <= RH_MAX:
                    vz.post(VZ_RH_PATH, rh)
                    print("Sent RH:", rh)
                else:
                    print("RH ignored (out of range):", rh)

            # Pressure comes from the BMP280, retried here if it failed at boot.
            if bmp is None:
                try:
                    bmp = init_bmp(i2c)
                    print("BMP280 recovered")
                except Exception as e:
                    log_exc("BMP280 still down:", e)

            if bmp is not None:
                try:
                    p = read_pressure(bmp)
                    if PRESS_MIN <= p <= PRESS_MAX:
                        vz.post(VZ_PRESS_PATH, p)
                        print("Sent p:", p)
                    else:
                        print("p ignored (out of range):", p)
                except Exception as e:
                    # Drop the handle so it gets re-initialised next cycle.
                    bmp = None
                    log_exc("Pressure read failed:", e)

            mem_err_count = 0  # a clean loop iteration -> clear the safety net

        except MemoryError as e:
            # Heap too fragmented for the socket buffer. Drop the connection so
            # next loop starts clean; if it keeps happening, only a reboot can
            # defragment (see MAX_CONSECUTIVE_MEM_ERRORS).
            vz.close()
            mem_err_count += 1
            log_exc("Loop MemoryError #{}:".format(mem_err_count), e)
            if mem_err_count >= MAX_CONSECUTIVE_MEM_ERRORS:
                log_exc("Too many consecutive MemoryErrors -> reboot", e)
                time.sleep(1)
                machine.reset()

        except Exception as e:
            log_exc("Loop error:", e)

        gc.collect()
        time.sleep(LOOP_PERIOD_S)

    vz.close()
    print("Reboot after {} hours".format(RUNTIME_HOURS))
    machine.reset()


main()
