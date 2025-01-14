#!/usr/bin/python3
"""Exports Dyson Pure Hot+Cool (DysonLink) statistics as Prometheus metrics."""

import argparse
import functools
import logging
import sys
import time
import threading

from typing import Callable, Dict, List

import prometheus_client
import libdyson
import libdyson.dyson_device
import libdyson.exceptions

import config
import metrics


class DeviceWrapper:
    """Wrapper for a config.Device.

    This class has two main purposes:
      1) To associate a device name & libdyson.DysonFanDevice together
      2) To start background thread that asks the DysonFanDevice for updated
         environmental data on a periodic basis.

    Args:
      device: a config.Device to wrap
      environment_refresh_secs: how frequently to refresh environmental data
    """

    def __init__(self, device: config.Device, environment_refresh_secs=30):
        self._config_device = device
        self._environment_refresh_secs = environment_refresh_secs
        self.libdyson = self._create_libdyson_device()

    @property
    def name(self) -> str:
        """Returns device name, e.g; 'Living Room'."""
        return self._config_device.name

    @property
    def serial(self) -> str:
        """Returns device serial number, e.g; AB1-XX-1234ABCD."""
        return self._config_device.serial

    @property
    def is_connected(self) -> bool:
        """True if we're connected to the Dyson device."""
        return self.libdyson.is_connected

    def connect(self, host: str, retry_on_timeout_secs: int = 30):
        """Connect to the device and start the environmental monitoring timer.

        Args:
          host: ip or hostname of Dyson device
          retry_on_timeout_secs: number of seconds to wait in between retries. this will block the running thread.
        """
        if self.is_connected:
            logging.info(
                'Already connected to %s (%s); no need to reconnect.', host, self.serial)
        else:
            try:
                self.libdyson.connect(host)
                self._refresh_timer()
            except libdyson.exceptions.DysonConnectTimeout:
                logging.error(
                    'Timeout connecting to %s (%s); will retry', host, self.serial)
                threading.Timer(retry_on_timeout_secs,
                                self.connect, args=[host]).start()

    def disconnect(self):
        """Disconnect from the Dyson device."""
        self.libdyson.disconnect()

    def _refresh_timer(self):
        timer = threading.Timer(self._environment_refresh_secs,
                                self._timer_callback)
        timer.start()

    def _timer_callback(self):
        if self.is_connected:
            logging.debug(
                'Requesting updated environmental data from %s', self.serial)
            try:
                self.libdyson.request_environmental_data()
            except AttributeError:
                logging.error('Race with a disconnect? Skipping an iteration.')
            self._refresh_timer()
        else:
            logging.debug('Device %s is disconnected.', self.serial)

    def _create_libdyson_device(self):
        return libdyson.get_device(self.serial, self._config_device.credentials,
                                   self._config_device.product_type)


class ConnectionManager:
    """Manages connections via manual IP or via libdyson Discovery.

    Args:
      update_fn: A callable taking a name, serial,
      devices: a list of config.Device entities
      hosts: a dict of serial -> IP address, for direct (non-zeroconf) connections.
    """

    def __init__(self, update_fn: Callable[[str, str, bool, bool], None],
                 devices: List[config.Device], hosts: Dict[str, str]):
        self._update_fn = update_fn
        self._hosts = hosts

        logging.info('Starting discovery...')
        self._discovery = libdyson.discovery.DysonDiscovery()
        self._discovery.start_discovery()

        for device in devices:
            self._add_device(DeviceWrapper(device))

    def _add_device(self, device: DeviceWrapper, add_listener=True):
        """Adds and connects to a device.

        This will connect directly if the host is specified in hosts at
        initialisation, otherwise we will attempt discovery via zeroconf.

        Args:
          device: a config.Device to add
          add_listener: if True, will add callback listeners. Set to False if
                        add_device() has been called on this device already.
        """
        if add_listener:
            callback_fn = functools.partial(self._device_callback, device)
            device.libdyson.add_message_listener(callback_fn)

        manual_ip = self._hosts.get(device.serial.upper())
        if manual_ip:
            logging.info('Attempting connection to device "%s" (serial=%s) via configured IP %s',
                         device.name, device.serial, manual_ip)
            device.connect(manual_ip)
        else:
            logging.info('Attempting to discover device "%s" (serial=%s) via zeroconf',
                         device.name, device.serial)
            callback_fn = functools.partial(self._discovery_callback, device)
            self._discovery.register_device(device.libdyson, callback_fn)

    @classmethod
    def _discovery_callback(cls, device: DeviceWrapper, address: str):
        # A note on concurrency: used with DysonDiscovery, this will be called
        # back in a separate thread created by the underlying zeroconf library.
        # When we call connect() on libpurecool or libdyson, that code spawns
        # a new thread for MQTT and returns. In other words: we don't need to
        # worry about connect() blocking zeroconf here.
        logging.info('Discovered %s on %s', device.serial, address)
        device.connect(address)

    def _device_callback(self, device, message):
        logging.debug('Received update from %s: %s', device.serial, message)
        if not device.is_connected:
            logging.info(
                'Device %s is now disconnected, clearing it and re-adding', device.serial)
            device.disconnect()
            self._discovery.stop_discovery()
            self._discovery.start_discovery()
            self._add_device(device, add_listener=False)
            return

        is_state = message == libdyson.MessageType.STATE
        is_environ = message == libdyson.MessageType.ENVIRONMENTAL
        self._update_fn(device.name, device.libdyson, is_state=is_state,
                        is_environmental=is_environ)


def _sleep_forever() -> None:
    """Sleeps the calling thread until a keyboard interrupt occurs."""
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            break


def main(argv):
    """Main body of the program."""
    parser = argparse.ArgumentParser(prog=argv[0])
    parser.add_argument('--port', help='HTTP server port',
                        type=int, default=8091)
    parser.add_argument(
        '--config', help='Configuration file (INI file)', default='config.ini')
    parser.add_argument(
        '--log_level', help='Logging level (DEBUG, INFO, WARNING, ERROR)', type=str, default='INFO')
    parser.add_argument(
        '--include_inactive_devices',
        help='Do not use; this flag has no effect and remains for compatibility only',
        action='store_true')
    args = parser.parse_args()

    try:
        level = getattr(logging, args.log_level)
    except AttributeError:
        print(f'Invalid --log_level: {args.log_level}')
        sys.exit(-1)
    args = parser.parse_args()

    logging.basicConfig(
        format='%(asctime)s [%(thread)d] %(levelname)10s %(message)s',
        datefmt='%Y/%m/%d %H:%M:%S',
        level=level)

    logging.info('Starting up on port=%s', args.port)

    if args.include_inactive_devices:
        logging.warning(
            '--include_inactive_devices is now inoperative and will be removed in a future release')

    try:
        cfg = config.Config(args.config)
    except:
        logging.exception('Could not load configuration: %s', args.config)
        sys.exit(-1)

    devices = cfg.devices
    if len(devices) == 0:
        logging.fatal(
            'No devices configured; please re-run this program with --create_device_cache.')
        sys.exit(-2)

    prometheus_client.start_http_server(args.port)

    ConnectionManager(metrics.Metrics().update, devices, cfg.hosts)

    _sleep_forever()


if __name__ == '__main__':
    main(sys.argv)
