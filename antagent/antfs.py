# Copyright (c) 2012, Braiden Kindt.
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 
#   1. Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
# 
#   2. Redistributions in binary form must reproduce the above
#      copyright notice, this list of conditions and the following
#      disclaimer in the documentation and/or other materials
#      provided with the distribution.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDER AND CONTRIBUTORS
# ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY
# WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import random
import struct
import collections
import logging
import time
import os
import socket

import antagent.ant as ant

_LOG = logging.getLogger("antagant.antfs")

ANTFS_HOST_ID = os.getpid() & 0xFFFFFFFF
ANTFS_HOST_NAME = socket.gethostname()[:8]

ANTFS_SEARCH_NETWORK_KEY = "\xa8\xa4\x23\xb9\xf5\x5e\x63\xc1"
ANTFS_SEARCH_FREQ = 50
ANTFS_SEARCH_PERIOD = 0x1000
ANTFS_SEARCH_CHANNEL_TIMEOUT = 255
ANTFS_SEARCH_SEARCH_WAVEFORM = 0x0053

ANTFS_TRANSPORT_ACK_RETRY = 4
ANTFS_TRANSPORT_FREQS =  [3, 7, 15, 20, 25, 29, 34, 40, 45, 49, 54, 60, 65, 70, 75, 80]
ANTFS_TRANSPORT_PERIOD = 0b100
ANTFS_TRANSPORT_CHANNEL_TIMEOUT = 2


class Beacon(object):

    DATA_PAGE_ID = 0x43
    STATE_LINK, STATE_AUTH, STATE_TRANSPORT, STATE_BUSY = range(0,4)

    __struct = struct.Struct("<BBBBI")

    @classmethod
    def unpack(cls, msg):
        if msg and ord(msg[0]) == Beacon.DATA_PAGE_ID:
            result = cls()
            result.data_page_id, result.status_1, result.status_2, result.auth_type, result.descriptor = cls.__struct.unpack(msg[:8])
            result.period = 0x07 & result.status_1
            result.pairing_enabled = 0x80 & result.status_1
            result.upload_enabled = 0x10 & result.status_1
            result.data_availible = 0x20 & result.status_1
            result.device_state = 0x0f & result.status_2
            result.data = msg[8:]
            return result

    def __str__(self):
        return self.__class__.__name__ + str(self.__dict__)


class Command(object):

    DATA_PAGE_ID = 0x44
    LINK, DISCONNECT, AUTH, PING, DIRECT = 0x02, 0x03, 0x04, 0x05, 0x0D

    __struct = struct.Struct("<BB6x")

    @classmethod
    def unpack(cls, msg):
        beacon = Beacon.unpack(msg) 
        if beacon and beacon.data and ord(beacon.data[0]) == Command.DATA_PAGE_ID:
            result = cls()
            result.beacon = beacon
            result.data_page_id, result.command_id = cls.__struct.unpack(beacon.data[:8])
            return result

    def __str__(self):
        return self.__class__.__name__ + str(self.__dict__)


class Disconnect(Command):
    
    COMMAND_ID = Command.DISCONNECT

    __struct = struct.Struct("<BB6x")

    def pack(self):
        return self.__struct.pack(self.DATA_PAGE_ID, self.COMMAND_ID)


class Link(Command):

    COMMAND_ID = Command.LINK 

    __struct = struct.Struct("<BBBBI")

    def __init__(self, frequency=None, period=ANTFS_TRANSPORT_PERIOD, host_id=ANTFS_HOST_ID):
        self.frequency = frequency if frequency is not None else random.choice(ANTFS_TRANSPORT_FREQS)
        self.period = period
        self.host_id = host_id

    def pack(self):
        return self.__struct.pack(self.DATA_PAGE_ID, self.COMMAND_ID, self.frequency, self.period, self.host_id)
    

class Auth(Command):

    COMMAND_ID = Command.AUTH

    OP_PASS_THRU, OP_CLIENT_SN, OP_PAIR, OP_PASSKEY = range(0, 4)
    RESPONSE_NA, RESPONSE_ACCEPT, RESPONSE_REJECT = range(0, 3)

    __struct = struct.Struct("<BBBBI")

    def __init__(self, op_id=None, auth_string="", host_id=ANTFS_HOST_ID):
        self.op_id = op_id
        self.auth_string = auth_string
        self.host_id = host_id

    def pack(self):
        return self.__struct.pack(self.DATA_PAGE_ID, self.COMMAND_ID, self.op_id, len(self.auth_string), self.host_id) + self.auth_string
    
    @classmethod
    def unpack(cls, msg):
        auth = super(Auth, cls).unpack(msg)
        if auth and auth.command_id & 0x7F == Auth.COMMAND_ID:
            data_page_id, command_id, auth.response_type, auth_string_length, auth.client_id = cls.__struct.unpack(auth.beacon.data[:8])
            auth.auth_string = auth.beacon.data[8:8 + auth_string_length]
            return auth

    
class Host(object):

    """
    Attempt to pair with devices even if beacon
    claims that device does not have pairing enabled.
    405CX beacon doesn't seem to set "pairing enabled" bit.
    """
    force_pairing = True

    def __init__(self, ant_session, known_client_keys=None):
        self.ant_session = ant_session
        self.known_client_keys = known_client_keys or {}

    def close(self):
        self.disconnect()
        self.ant_session.close()

    def disconnect(self):
        try:
            beacon = Beacon.unpack(self.channel.recv_broadcast(.5))
        except ant.AntTimeoutError:
            pass
        else:
            if beacon.device_state != Beacon.STATE_LINK:
                self.channel.write(Disconnect().pack())

    def search(self, search_timeout=60):
        """
        Return the first device found which is either availible for
        parinf or has data ready for download. Multiple calls will
        restart search. When multiple devices are in range, the channel
        will non-determisctially track to the first device it finds.
        If you are search for a specific device you need to keep calling
        this method until you get the one your looking for.

        This operation is passive, and does not impack battery usage
        of the GPS device. e.g. it is OK to run search in an infinite
        loop to automatically upload data from devices within range.
        """
        timeout = time.time() + search_timeout
        while time.time() < timeout:
            try:
                # if we didn't find a device, maybe another is in range?
                # restart search every time. Once a device is tracking
                # we don't get any more hits. So, just keep re-openning
                # channel until we find device we're looking for.
                # TODO could implement AP2 filters, but this logic maintains
                # support for older devices.
                self._open_antfs_search_channel()
                # wait to recv beacon from device
                beacon = Beacon.unpack(self.channel.recv_broadcast(timeout=timeout - time.time()))
            except ant.AntTimeoutError:
                # ignore timeout error
                pass
            else:
                # check if event was a beacon
                if beacon:
                    _LOG.debug("Got ANT-FS Beacon. %s", beacon)
                    # and if device is a state which will accept our link
                    if  beacon.device_state != Beacon.STATE_LINK:
                        _LOG.warning("Device busy, not ready for link. client_id=0x%08x state=%d.",
                                beacon.descriptor, beacon.device_state)
                    elif not beacon.data_availible:
                        _LOG.info("Found device, but no new data for download. client_id=0x%08x",
                                beacon.descriptor)
                    else:
                        # adjust message period to match beacon
                        self._configure_antfs_period(beacon.period)
                        return beacon
        
    def link(self):
        """
        Atempt to create an ANTFS link with the device
        who's beacon was most recently returned by search().
        If this channel is not tracking, the operation will
        block until a device is found, and attempt a link.
        Operation will raise a timeout exception if device
        does not reply in time our if an attempt was made
        to link while channel was not tracking.
        """
        # send the link commmand
        link = Link()
        self.channel.send_acknowledged(link.pack(), retry=10)
        # change this channels frequency to match link
        self._configure_antfs_transport_channel(link)
        # block indefinately for the antfs beacon on new freq.
        # (don't need a timeout since channel will auto close if device lost)
        beacon = Beacon.unpack(self.channel.recv_broadcast(0))
        # device should be broadcasting our id and ready to accept auth
        assert beacon.device_state == Beacon.STATE_AUTH and beacon.descriptor == ANTFS_HOST_ID

    def auth(self, timeout=60):
        """
        Attempt to create an authenticated transport
        with the device we are currenly linked. Not
        valid unless device is in link status.
        If a client key is known, transport will be
        openned without user interaction. If key is unkown
        we will attempt to pair with device (which must
        be acknowledged by human on GPS device.)
        If paising is not enabled Auth is impossible.
        Error raised if auth is not successful.
        Timeout only applies user interaction during pairing process.
        """
        # get the S/N of client device
        auth_cmd = Auth(Auth.OP_CLIENT_SN)
        self.channel.write(auth_cmd.pack())
        while True:
            auth_reply = Auth.unpack(self.channel.read())
            if auth_reply: break
        _LOG.debug("Got client auth string. %s", auth_reply)
        # check if the auth key for this device is known
        client_id = auth_reply.client_id
        key = self.known_client_keys.get(client_id, None)
        if key:
            pass
        elif auth_reply.beacon.pairing_enabled or self.force_pairing:
            auth_cmd = Auth(Auth.OP_PAIR, ANTFS_HOST_NAME)
            self.channel.write(auth_cmd.pack())
            try:
                auth_reply = Auth.unpack(self.channel.read(timeout))
            except ant.AntTimeoutError:
                pass
            else:
                if auth_reply and auth_reply.response_type == Auth.RESPONSE_ACCEPT:
                    _LOG.debug("Device paired. key=%s", auth_reply.auth_string.encode("hex"))
                    self.known_client_keys[client_id] = auth_reply.auth_string
                else:
                    _LOG.debug("Device pairing failed. Request rejected?")
        else:
            _LOG.warning("Device 0x08%x has data but pairing is disabled and key is unkown.", client_id)
        #confirm the ANT-FS channel is open
        beacon = Beacon.unpack(self.channel.recv_broadcast(0))
        assert beacon.device_state == Beacon.STATE_TRANSPORT and beacon.descriptor == ANTFS_HOST_ID

    def _open_antfs_search_channel(self):
        self.ant_session.open()
        self.ant_session.reset_system()
        self.channel = self.ant_session.channels[0]
        self.network = self.ant_session.networks[0]
        self._configure_antfs_search_channel()
        self.channel.open()

    def _configure_antfs_search_channel(self):
        self.network.set_key(ANTFS_SEARCH_NETWORK_KEY)
        self.channel.assign(channel_type=0x00, network_number=self.network.network_number)
        self.channel.set_id(device_number=0, device_type_id=0, trans_type=0)
        self.channel.set_period(ANTFS_SEARCH_PERIOD)
        self.channel.set_search_timeout(ANTFS_SEARCH_CHANNEL_TIMEOUT)
        self.channel.set_rf_freq(ANTFS_SEARCH_FREQ)
        self.channel.set_search_waveform(ANTFS_SEARCH_SEARCH_WAVEFORM)

    def _configure_antfs_transport_channel(self, link):
        self.channel.set_rf_freq(link.frequency)
        self.channel.set_search_timeout(ANTFS_TRANSPORT_CHANNEL_TIMEOUT)
        self._configure_antfs_period(link.period)

    def _configure_antfs_period(self, period):
        period_hz = 2 ** (period - 1)
        channel_period = 0x8000 / period_hz
        self.channel.set_period(channel_period)
        

# vim: ts=4 sts=4 et
