# This file is executed on every boot (including wake-boot from deepsleep)
import esp
import machine
import time
import urequests
import time, ntptime
import machine
# import azbme680
from bmp280 import *
import network
import socket
from config import WIFI_SSID, WIFI_PASSWORD
#esp.osdebug(None)
#import webrepl
#webrepl.start()


def do_connect():
    sta_if = network.WLAN(network.STA_IF)
    if not sta_if.isconnected():
        print('connecting to network...')
        sta_if.active(True)
        sta_if.connect(WIFI_SSID, WIFI_PASSWORD)
        while not sta_if.isconnected():
            pass
    print('network config:', sta_if.ifconfig())
    
# Connect to wifi
do_connect()

# Get IP of volkszaehler
addr_info = socket.getaddrinfo("volkszaehler-in", 80)
# print(addr_info)
ip = socket.getaddrinfo("volkszaehler-in", 80)[0][-1][0]
print("IP of volkszaehler is", ip)

# Set NTP time
ntptime.settime()

# Save boot time
now = time.ticks_ms()

# Define I2C bus
i2c = machine.I2C(0, scl=machine.Pin(22), sda=machine.Pin(21))
bmp = BMP280(i2c)

# Scan I2C bus
print('Scan i2c bus...')
devices = i2c.scan()

if len(devices) == 0:
  print("No i2c device !")
else:
  print('i2c devices found:',len(devices))

  for device in devices:  
    print("Decimal address: ",device," | Hexa address: ",hex(device))
    
    
#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# This program reads out IST HYT sensors(HYT-221, HYT-271, HYT-939)
#
# License: Public Domain/CC0
# Original source: https://github.com/joppiesaus/python-hyt/
#
# +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Humidity:
#   0x0000      -       0x3FFFF  hex
#   0           -       16383    dec
#   0           -       100      % RH
#
# Temperature:
#   0x0000      -       0x3FFF   hex
#   0           -       16383    dec
#  -40          -       125      °C
#   233.15      -       298.15   °K
#   ???         -       ???      °F
#
#  |  byte 0  |  byte 1  |  byte 2  |  byte 3  |  
#  |---------------------|---------------------|
#  |      Humidity       |     Temperature     |
#  |---------------------|---------------------|
#  | 2 bit |   14 bit    |   14 bit    | 2 bit |
#  |-------|-------------|-------------|-------|
#  | state |    data     |    data     | dummy |
#      |
#      +-----------------------+
#      |   bit 0   |   bit 1   |
#      | CMode bit | stale bit |
#      +-----------------------+
#
# CMode bit: if 1 - sensor is in "command mode"
# Stale bit: if 1 - no new value has been created since the last reading
#
# RH = (100 / (2^14 - 1)) * RHraw
# T  = (165 / (2^14 - 1)) * Traw - 40
#
# crappy ascii picture from top to see pinout:
#  ---
# |~ ~|
# | O |
# |___|
# ||||
# |||+-SCL
# ||+--VDD
# |+---GND
# +----SDA
#
# +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++



addr = 0x28 # default address for hyt sensors
delay = 100.0 / 1000.0 # 50-60 ms delay. Without delay, it doesn't work.
# I should test that delay more.
# bus = smbus.SMBus(1) # use /dev/i2c1

# Initialize BMP280
bmp.use_case(BMP280_CASE_WEATHER)
bmp.oversample(BMP280_OS_HIGH)

bmp.temp_os = BMP280_TEMP_OS_8
bmp.press_os = BMP280_PRES_OS_4

bmp.standby = BMP280_STANDBY_250
bmp.iir = BMP280_IIR_FILTER_2

# bmp.spi3w = BMP280_SPI3W_ON

bmp.power_mode = BMP280_POWER_FORCED



def read():
	i2c.writeto(addr, b'0x00') # send some stuff
	time.sleep(delay) # wait a bit
	reading = i2c.readfrom(addr, 4) # read the bytes
	# Mask the first two bits
	humidity = ((reading[0] & 0x3F) * 0x100 + reading[1]) * (100.0 / 16383.0)
	# Mask the last two bits, shift 2 bits to the right
	temperature = 165.0 / 16383.0 * ((reading[2] * 0x100 + (reading[3] & 0xFC)) >> 2) - 40
	return humidity, temperature

def pressure():
        try:
            bmp.force_measure()
        except:
            print("BMP Force measure not working.")
            
        while bmp.is_measuring:
            time.sleep(0.1)
            
        while bmp.is_updating:
            time.sleep(0.1)
            
        # print(bmp.temperature)
        # print(bmp.pressure / 100 + (360/10*1.22))

        bmp.sleep()
        # return bmp.pressure / 100 + (360/10*1.22) # QNH Wert
        return bmp.pressure / 100


def readandprint():
	rh, t = read()
	print("Humidity:", rh, "% RH")
	print("Temperature:", t, "°C")

def volkszaehler():
	rh, t = read()
	try:
		p = pressure()
	except:
		print("Getting pressure failed.")

	try:
		url = "http://"+ip+"/middleware/data/3bcaceb0-6543-11ee-8290-9fb1c7c0202b.json?operation=add&value="+str(t)
		print(url)
		urequests.post(url)
		# urequests.post("http://192.168.178.3/middleware/data/b628e700-6543-11ee-8365-e1ea2c002e32.json?operation=add&value="+str(rh))
		urequests.post("http://"+ip+"/middleware/data/df0f3640-6543-11ee-84f3-e9c4bed13213.json?operation=add&value="+str(p))
		# 	df0f3640-6543-11ee-84f3-e9c4bed13213 Druck
		print("Send following values to Volkszaehler: ", t, rh,p)
	except:
		print("Could not upload data to volkszähler middleware.")

if __name__ == "__main__":
    readandprint()
    print(pressure())
    while ((time.ticks_ms()-now) / 1000 / 60 / 60)  <= 6:
        if network.WLAN(network.STA_IF).isconnected() == False:
            try:
                do_connect()
            except:
                pass
            
        print("Time since last reboot: ", (time.ticks_ms()-now) / 1000 / 60)
        # readandprint()
        # print(pressure())
        try:
            volkszaehler()
        except:
            print("Update to Volkszähler crashed")
            
        try:
            ntptime.settime()
        except:
            print("Updating NTP time failed")
            
        time.sleep(60) # wait 60 sec.
        
    # Do hard reboot
    print("Doing hard reboot.")
    machine.reset()
