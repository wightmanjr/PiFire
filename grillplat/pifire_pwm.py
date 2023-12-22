#!/usr/bin/env python3

# *****************************************
# PiFire PWM PCB Platform Interface Library
# *****************************************
#
# Description: This library supports controlling the PiFire PWM PCB, which
# integrates solid state relays and PWM amplifier circuitry to drive a 12V DC PWM fan.
#
# Relays (augur, igniter) are controlled via GPIO pins.
#
# 12V power to the fan is controlled via GPIO, which controls a power transistor.
#
# A 3.3v PWM fan signal is generated by the RPi Hardware PWM module (PWM1 / GPIO 13).
# This 3.3v PWM signal controls an amplifying transistor that supplies 5v, and the design
# of this amplifying circuit inverts the logic, meaning a 100% PWM signal from the RPi will
# result in a 0% PWM signal at the 5V output to the fan. The code addresses this.
#
# *****************************************
#
# TODO - Fix debug logging to only toggle when debug is enabled
#
# TODO - Rewrite all functions/variables to use the following nomenclature:
#     PWM duty cycle - the actual "percent high" waveform parameter sent to the PWM generator
#     Fan percent (or "fan duty cycle") - the requested fan speed percentage
#     Since the PWM board's amplifier inverts the PWM signal logic, technically:
#	PWM duty cycle = (100 - fan percent speed / fan duty cycle)
#	Fan percent speed = (100 - PWM duty cycle)
#
# *****************************************
# Imported Libraries
# *****************************************

import subprocess
from gpiozero import OutputDevice
from gpiozero import Button
from gpiozero.threads import GPIOThread
from rpi_hardware_pwm import HardwarePWM
import logging

class GrillPlatform:

	def __init__(self, out_pins, in_pins, trigger_level='LOW', dc_fan=False, frequency=100):
		self.logger = logging.getLogger("events")
		self.logger.info('grillplat pifire_pwm __init__: ************************************************')
		self.logger.info('grillplat pifire_pwm __init__: **** Starting Grill Platform Initialization ****')
		self.logger.info('grillplat pifire_pwm __init__: ************************************************')
		self.out_pins = out_pins # { 'power' : 4, 'auger' : 14, 'fan' : 15, 'dc_fan' : 26, 'igniter' : 18, 'pwm' : 13 }
		self.in_pins = in_pins # { 'selector' : 17 }
		self.dc_fan = dc_fan
		self.frequency = frequency
		self.current = {}

		self.selector = Button(self.in_pins['selector'])

		active_high = trigger_level == 'HIGH'

		if dc_fan:
			self.current_fan_speed_percent = 100 # Hardware PWM library does not have a mechanism to retrieve the current duty cycle - initialize a variable to track this
			self._ramp_thread = None
			self.fan = OutputDevice(self.out_pins['dc_fan'], active_high=active_high, initial_value=False)
			self.hardware_pwm_channel = 1 # PiFire PWM PCB uses GPIO 13 for PWM signal generation, which maps to Hardware PWM channel 1
			self.pwm = HardwarePWM(pwm_channel=self.hardware_pwm_channel, hz=self.frequency)
			self.logger.debug('grillplat pifire_pwm __init__: Hardware PWM setup: Using PWM channel ' + str(self.hardware_pwm_channel) + ' and PWM frequency ' + str(self.frequency))

		else:
			self.fan = OutputDevice(self.out_pins['fan'], active_high=active_high, initial_value=False)

		self.auger = OutputDevice(self.out_pins['auger'], active_high=active_high, initial_value=False)
		self.igniter = OutputDevice(self.out_pins['igniter'], active_high=active_high, initial_value=False)
		self.power = OutputDevice(self.out_pins['power'], active_high=active_high, initial_value=False)

	def auger_on(self):
		self.logger.debug('grillplat pifire_pwm auger_on: Turning on augur')
		self.auger.on()

	def auger_off(self):
		self.logger.debug('grillplat pifire_pwm auger_off: Turning off augur')
		self.auger.off()

	def fan_on(self, fan_speed_percent=100):
		self.fan.on() # Turn on fan output pin to enable fan power
		if self.dc_fan:
			self._stop_ramp()
			self.logger.debug('grillplat pifire_pwm fan_on: Turning on PWM fan with fan speed percent ' + str(fan_speed_percent))
			start_duty_cycle = float(100 - fan_speed_percent) # PWM duty cycle = (100 - fan percent speed)
			self.pwm.start(start_duty_cycle) # Hardware PWM needs to have a start() before we can change_duty_cycle() later
			self.current_fan_speed_percent = fan_speed_percent # Keep track of our current fan percent speed

	def fan_off(self):
		self.fan.off()
		if self.dc_fan:
			self.logger.debug('grillplat pifire_pwm fan_off: Turning off PWM fan')
			self.pwm.stop()
			self.current_fan_speed_percent = 0 # Fan is off, so our current fan speed is now 0

	def fan_toggle(self):
		self.fan.toggle()

	def set_duty_cycle(self, fan_speed_percent, override_ramping=True):
		# This can be called by both control.py and the thread that handles PWM fan ramping (for Smoke Plus).
		# If control.py is doing the calling, then we want to override the ramping thread, so we need to stop it so the PWM change will stick.
		# If the ramp thread is doing the calling, then we set override_ramping to False when calling, so we don't end up stopping the thread we are in.
		if override_ramping:
			self._stop_ramp()
		self.logger.debug('grillplat pifire_pwm set_duty_cycle: Changing fan speed percent to ' + str(fan_speed_percent))
		pwm_duty_cycle = float(100 - fan_speed_percent) # Duty cycle is inverted due to PWM board amplifier circuitry
		self.pwm.change_duty_cycle(pwm_duty_cycle) # Hardware PWM library simply takes duty cycle in percent
		self.current_fan_speed_percent = fan_speed_percent # Keep track of our current fan percent speed

	def pwm_fan_ramp(self, on_time=5, min_duty_cycle=20, max_duty_cycle=100):
		self.fan.on()
		self.logger.debug('grillplat pifire_pwm pwm_fan_ramp: Starting fan ramp: on_time: ' + str(on_time) + ' min_duty_cycle: ' + str(min_duty_cycle) + ' max_duty_cycle: ' + str(max_duty_cycle))
		self._start_ramp(on_time=on_time, min_duty_cycle=min_duty_cycle, max_duty_cycle=max_duty_cycle)

	def set_pwm_frequency(self, frequency=30):
		self.logger.debug('grillplat pifire_pwm set_pwm_frequency: Setting PWM signal frequency to ' + str(frequency))
		self.pwm.change_frequency(frequency)

	def igniter_on(self):
		self.logger.debug('grillplat pifire_pwm igniter_on: Turning on igniter')
		self.igniter.on()

	def igniter_off(self):
		self.logger.debug('grillplat pifire_pwm igniter_off: Turning off igniter')
		self.igniter.off()

	def power_on(self):
		self.logger.debug('grillplat pifire_pwm power_on: Powering on grill platform')
		self.power.on()

	def power_off(self):
		self.logger.debug('grillplat pifire_pwm power_off: Powering off grill platform')
		self.power.off()

	def get_input_status(self):
		return self.selector.is_active

	def get_output_status(self):
		self.current = {}
		self.current['auger'] = self.auger.is_active
		self.current['igniter'] = self.igniter.is_active
		self.current['power'] = self.power.is_active
		self.current['fan'] = self.fan.is_active
		if self.dc_fan:
#			self.logger.debug('grillplat pifire_pwm get_output_status: self.current_fan_speed_percent = ' + str(self.current_fan_speed_percent)) # This is a little verbose, even for debug logging
			self.current['pwm'] = self.current_fan_speed_percent
			self.current['frequency'] = self.frequency
		return self.current

	def _start_ramp(self, on_time, min_duty_cycle, max_duty_cycle, background=True):
		self._stop_ramp()
		self.logger.debug('grillplat pifire_pwm _start_ramp: Setting starting fan percentage for ramp: min_duty_cycle: ' + str(min_duty_cycle))
		self.logger.debug('grillplat pifire_pwm _start_ramp: Starting fan ramp thread: on_time: ' + str(on_time) + ' min_duty_cycle: ' + str(min_duty_cycle) + ' max_duty_cycle: ' + str(max_duty_cycle))
		min_fan_percent = min_duty_cycle # Keeping things sane, the setting passed in is actually percent, not the eventual PWM duty cycle
		self.fan_on(min_fan_percent) # Need to turn on PWM with starting percentage
		self._ramp_thread = GPIOThread(self._ramp_device, (on_time, min_duty_cycle, max_duty_cycle))
		self._ramp_thread.start()
		if not background:
			self._ramp_thread.join()
			self._ramp_thread = None

	def _stop_ramp(self):
		self.logger.debug('grillplat pifire_pwm _stop_ramp: Stopping fan ramp')
		if self._ramp_thread:
			self._ramp_thread.stop()
			self._ramp_thread = None

	def _ramp_device(self, on_time, min_duty_cycle, max_duty_cycle, fps=25):
		duty_cycle = max_duty_cycle / 100
		self.logger.debug('grillplat pifire_pwm _ramp_device: Fan ramp thread calculating / executing')
		sequence = []
		sequence += [
			(1 - (i * (duty_cycle / fps) / on_time), 1 / fps)
			for i in range(int((fps * on_time) * (min_duty_cycle / max_duty_cycle)), int(fps * on_time))
		]
		sequence.append((1.0 - duty_cycle, 1 / fps))

		for value, delay in sequence:
			new_duty_cycle = round(value, 4) * 100 # PWM duty cycle is 0-100 for Hardware PWM, sequence above generates 0.00-1.00, so multiply by 100
			fan_speed_percent = float(100 - new_duty_cycle) # Duty cycle is inverted due to PWM amplifier
#			self.logger.debug('grillplat pifire_pwm _ramp_device: Changing fan speed percent to ' + str(fan_speed_percent)) # Redundant
			# Call self.set_duty_cycle but set extra override_ramping parameter to False so we don't kill the thread
			self.set_duty_cycle(fan_speed_percent,False) # set_duty_cycle takes fan speed percent, not actual PWM duty cycle
			self.current_fan_speed_percent = fan_speed_percent # Keep track of our current fan percent speed
			if self._ramp_thread.stopping.wait(delay):
				break

	def check_throttled():
		"""Checks for under-voltage and throttling using vcgencmd.

		Returns:
			(bool, bool): A tuple of (under_voltage, throttled) indicating their status.
		"""

		output = subprocess.check_output(["vcgencmd", "get_throttled"])
		status_str = output.decode("utf-8").strip()[10:]  # Extract the numerical value
		status_int = int(status_str, 16)  # Convert from hex to decimal

		under_voltage = bool(status_int & 0x10000)  # Check bit 16 for under-voltage
		throttled = bool(status_int & 0x5)  # Check bits 0 and 2 for active throttling

		return under_voltage, throttled
	

	def check_wifi_quality():
		"""Checks the Wi-Fi signal quality on a Raspberry Pi and returns the percentage value (or None if not connected)."""

		try:
			# Use iwconfig to get the signal quality
			output = subprocess.check_output(["iwconfig", "wlan0"])
			lines = output.decode("utf-8").splitlines()

			# Find the line containing "Link Quality" and extract the relevant part
			for line in lines:
				if "Link Quality=" in line:
					quality_str = line.split("=")[1].strip()  # Isolate the part after "="
					quality_parts = quality_str.split(" ")[0]  # Extract only the first part before spaces

					try:
						quality_value, quality_max = quality_parts.split("/")  # Split for numerical values
						percentage = (int(quality_value) / int(quality_max)) * 100
						return round(percentage, 2)  # Round to two decimal places

					except ValueError:
						# Handle cases where the value might not be directly convertible to an integer
						return None

		except subprocess.CalledProcessError:
			# Handle errors, such as iwconfig not being found or wlan0 not existing
			pass

		# Return None if not connected or if there was an error
		return None
