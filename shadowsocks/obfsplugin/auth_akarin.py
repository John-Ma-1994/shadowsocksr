#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2018-2018 Akkariin
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, print_function, \
    with_statement

import hashlib
import logging
import binascii
import base64
import time
import datetime
import random
import math
import struct
import hmac
import bisect
import typing
from ..obfs import (server_info as ServerInfo)

import shadowsocks
from shadowsocks import common, lru_cache, encrypt
from shadowsocks.obfsplugin import plain
from shadowsocks.common import to_bytes, to_str, ord, chr
from shadowsocks.crypto import openssl

rand_bytes = openssl.rand_bytes


def create_auth_akarin_rand(method):
    return auth_akarin_rand(method)


def create_auth_akarin_spec_a(method):
    return auth_akarin_spec_a(method)


obfs_map: typing.Dict[str, tuple] = {
    'auth_akarin_rand': (create_auth_akarin_rand,),
    'auth_akarin_spec_a': (create_auth_akarin_spec_a,),
}


class xorshift128plus(object):
    max_int = (1 << 64) - 1
    mov_mask = (1 << (64 - 23)) - 1

    def __init__(self):
        self.v0 = 0
        self.v1 = 0

    def next(self) -> int:
        x = self.v0
        y = self.v1
        self.v0 = y
        x ^= ((x & xorshift128plus.mov_mask) << 23)
        x ^= (y ^ (x >> 17) ^ (y >> 26))
        self.v1 = x
        return (x + y) & xorshift128plus.max_int

    def init_from_bin(self, bin: bytes):
        if len(bin) < 16:
            bin += b'\0' * 16
        self.v0 = struct.unpack('<Q', bin[:8])[0]
        self.v1 = struct.unpack('<Q', bin[8:16])[0]

    def init_from_bin_len(self, bin: bytes, length: int):
        if len(bin) < 16:
            bin += b'\0' * 16
        self.v0 = struct.unpack('<Q', struct.pack('<H', length) + bin[2:8])[0]
        self.v1 = struct.unpack('<Q', bin[8:16])[0]


def match_begin(str1: str, str2: str):
    if len(str1) >= len(str2):
        if str1[:len(str2)] == str2:
            return True
    return False


class auth_base(plain.plain):
    def __init__(self, method: str):
        super(auth_base, self).__init__(method)
        self.method: str = method
        self.no_compatible_method = ''
        self.overhead: int = 4
        self.raw_trans: bool = False

    def init_data(self):
        return ''

    def get_overhead(self, direction: bool) -> int:  # direction: true for c->s false for s->c
        return self.overhead

    def set_server_info(self, server_info: ServerInfo):
        self.server_info: ServerInfo = server_info

    def client_encode(self, buf: bytes) -> bytes:
        return buf

    def client_decode(self, buf: bytes) -> typing.Tuple[bytes, bool]:
        return (buf, False)

    def server_encode(self, buf: bytes) -> bytes:
        return buf

    def server_decode(self, buf: bytes) -> typing.Tuple[bytes, bool, bool]:
        return (buf, True, False)

    def not_match_return(self, buf: bytes) -> typing.Tuple[bytes, bool]:
        self.raw_trans = True
        self.overhead = 0
        if self.method == self.no_compatible_method:
            return (b'E' * 2048, False)
        return (buf, False)


class client_queue(object):
    def __init__(self, begin_id: int):
        self.front: int = begin_id - 64
        self.back: int = begin_id + 1
        self.alloc: typing.Dict[int, bool] = {}
        self.enable: bool = True
        self.last_update: float = time.time()
        self.ref: int = 0

    def update(self):
        self.last_update = time.time()

    def addref(self):
        self.ref += 1

    def delref(self):
        if self.ref > 0:
            self.ref -= 1

    def is_active(self):
        return (self.ref > 0) and (time.time() - self.last_update < 60 * 10)

    def re_enable(self, connection_id: int):
        self.enable = True
        self.front = connection_id - 64
        self.back = connection_id + 1
        self.alloc = {}

    def insert(self, connection_id: int) -> bool:
        if not self.enable:
            logging.warn('obfs auth: not enable')
            return False
        if not self.is_active():
            self.re_enable(connection_id)
        self.update()
        if connection_id < self.front:
            logging.warn('obfs auth: deprecated id, someone replay attack')
            return False
        if connection_id > self.front + 0x4000:
            logging.warn('obfs auth: wrong id')
            return False
        if connection_id in self.alloc:
            logging.warn('obfs auth: duplicate id, someone replay attack')
            return False
        if self.back <= connection_id:
            self.back = connection_id + 1
        self.alloc[connection_id] = 1
        while (self.front in self.alloc) or self.front + 0x1000 < self.back:
            if self.front in self.alloc:
                del self.alloc[self.front]
            self.front += 1
        self.addref()
        return True


class obfs_auth_akarin_data(object):
    def __init__(self, name: str):
        self.name: str = name
        self.user_id: typing.Dict[int, lru_cache.LRUCache[int, client_queue]] = {}
        self.local_client_id: bytes = b''
        self.connection_id: int = 0
        self.max_client: int = 0
        self.max_buffer: int = 0
        self.set_max_client(64)  # max active client count

    def update(self, user_id: int, client_id: int, connection_id: int):
        if user_id not in self.user_id:
            self.user_id[user_id] = lru_cache.LRUCache()
        local_client_id: lru_cache.LRUCache[int, client_queue] = self.user_id[user_id]

        if client_id in local_client_id:
            local_client_id[client_id].update()

    def set_max_client(self, max_client: int):
        self.max_client: int = max_client
        self.max_buffer: int = max(self.max_client * 2, 1024)

    def insert(self, user_id: int, client_id: int, connection_id: int):
        if user_id not in self.user_id:
            self.user_id[user_id] = lru_cache.LRUCache()
        local_client_id: lru_cache.LRUCache[int, client_queue] = self.user_id[user_id]

        if local_client_id.get(client_id, None) is None or not local_client_id[client_id].enable:
            if local_client_id.first() is None or len(local_client_id) < self.max_client:
                if client_id not in local_client_id:
                    # TODO: check
                    local_client_id[client_id] = client_queue(connection_id)
                else:
                    local_client_id[client_id].re_enable(connection_id)
                return local_client_id[client_id].insert(connection_id)

            if not local_client_id[local_client_id.first()].is_active():
                del local_client_id[local_client_id.first()]
                if client_id not in local_client_id:
                    # TODO: check
                    local_client_id[client_id] = client_queue(connection_id)
                else:
                    local_client_id[client_id].re_enable(connection_id)
                return local_client_id[client_id].insert(connection_id)

            logging.warn(self.name + ': no inactive client')
            return False
        else:
            return local_client_id[client_id].insert(connection_id)

    def remove(self, user_id, client_id):
        if user_id in self.user_id:
            local_client_id: lru_cache.LRUCache[int, client_queue] = self.user_id[user_id]
            if client_id in local_client_id:
                local_client_id[client_id].delref()


class auth_akarin_rand(auth_base):
    def __init__(self, method: str):
        super(auth_akarin_rand, self).__init__(method)
        self.hashfunc: function = hashlib.md5
        self.recv_buf: bytes = b''
        self.unit_len: int = 2800
        self.raw_trans: bool = False
        self.has_sent_header: bool = False
        self.has_recv_header: bool = False
        self.client_id: int = 0
        self.connection_id: int = 0
        self.max_time_dif: int = 60 * 60 * 24  # time dif (second) setting
        self.salt: bytes = b"auth_akarin_rand"
        self.no_compatible_method: str = 'auth_akarin_rand'
        self.pack_id: int = 1
        self.recv_id: int = 1
        self.user_id: bytes = None
        self.user_id_num: int = 0
        self.user_key: bytes = None
        self.overhead: int = 4
        self.client_over_head: int = self.overhead
        self.last_client_hash: bytes = b''
        self.last_server_hash: bytes = b''
        self.random_client: xorshift128plus = xorshift128plus()
        self.random_server: xorshift128plus = xorshift128plus()
        self.encryptor: encrypt.Encryptor = None
        self.new_send_tcp_mss: int = 2000
        self.send_tcp_mss: int = 2000
        self.recv_tcp_mss: int = 2000
        self.send_back_cmd: typing.List[bytes] = []

    def init_data(self) -> obfs_auth_akarin_data:
        return obfs_auth_akarin_data(self.method)

    def get_overhead(self, direction: bool) -> int:  # direction: true for c->s false for s->c
        return self.overhead

    def set_server_info(self, server_info: ServerInfo):
        self.server_info = server_info
        try:
            max_client = int(server_info.protocol_param.split('#')[0])
        except:
            max_client = 64
        self.server_info.data.set_max_client(max_client)

    def trapezoid_random_float(self, d):
        if d == 0:
            return random.random()
        s = random.random()
        a = 1 - d
        return (math.sqrt(a * a + 4 * d * s) - a) / (2 * d)

    def trapezoid_random_int(self, max_val, d):
        v = self.trapezoid_random_float(d)
        return int(v * max_val)

    def send_rnd_data_len(self, buf_size: int, last_hash: bytes, random: xorshift128plus) -> int:
        if buf_size + self.server_info.overhead > self.send_tcp_mss:
            random.init_from_bin_len(last_hash, buf_size)
            return random.next() % 521
        if buf_size >= 1440 or buf_size + self.server_info.overhead == self.send_tcp_mss:
            return 0
        random.init_from_bin_len(last_hash, buf_size)
        if buf_size > 1300:
            return random.next() % 31
        if buf_size > 900:
            return random.next() % 127
        if buf_size > 400:
            return random.next() % 521
        return random.next() % (self.send_tcp_mss - buf_size - self.server_info.overhead)

    def recv_rnd_data_len(self, buf_size, last_hash, random: xorshift128plus) -> int:
        if buf_size + self.server_info.overhead > self.recv_tcp_mss:
            random.init_from_bin_len(last_hash, buf_size)
            return random.next() % 521
        if buf_size >= 1440 or buf_size + self.server_info.overhead == self.send_tcp_mss:
            return 0
        random.init_from_bin_len(last_hash, buf_size)
        if buf_size > 1300:
            return random.next() % 31
        if buf_size > 900:
            return random.next() % 127
        if buf_size > 400:
            return random.next() % 521
        return random.next() % (self.recv_tcp_mss - buf_size - self.server_info.overhead)

    def udp_rnd_data_len(self, last_hash, random: xorshift128plus) -> int:
        random.init_from_bin(last_hash)
        return random.next() % 127

    def rnd_data(self, buf_size: int, buf: bytes, last_hash: bytes, random: xorshift128plus) -> bytes:
        rand_len = self.send_rnd_data_len(buf_size, last_hash, random)

        rnd_data_buf = rand_bytes(rand_len)

        if buf_size == 0:
            return rnd_data_buf
        else:
            if rand_len > 0:
                return buf + rnd_data_buf
            else:
                return buf

    def pack_client_data(self, buf: bytes) -> bytes:
        """

        pack_data with_cmd=
        |ushort(0xff00^ushort(last_client_hash[14:16]))|
        |ushort(len(origin data)^ushort(last_client_hash[12:14]))|encrypt(origin data)|

        pack_data without_cmd=
        |ushort(len(origin data)^ushort(last_client_hash[14:16]))|encrypt(origin data)|

        HMAC_key=
        |user_key|uint(last_pack_id)|

        next_client_hash=
        HMAC_sign(key:(HMAC_key), be sign data:(pack_data))

        next_pack_id=
        uint((pack_id + 1)&0xFFFFFFFF)

        pack_out=
        |(pack_data)|next_client_hash|


        :param buf: bytes   origin data
        :return: bytes      packed data
        """
        buf = self.encryptor.encrypt(buf)
        if self.send_back_cmd:
            cmd_len = 2
            self.send_tcp_mss = self.recv_tcp_mss
            data = self.rnd_data(len(buf) + cmd_len, buf, self.last_client_hash, self.random_client)
            length = len(buf) ^ struct.unpack('<H', self.last_client_hash[12:14])[0]
            data = struct.pack('<H', length) + data
            length = 0xff00 ^ struct.unpack('<H', self.last_client_hash[14:])[0]
            data = struct.pack('<H', length) + data
        else:
            data = self.rnd_data(len(buf), buf, self.last_client_hash, self.random_client)
            length = len(buf) ^ struct.unpack('<H', self.last_client_hash[14:])[0]
            data = struct.pack('<H', length) + data
        mac_key = self.user_key + struct.pack('<I', self.pack_id)
        self.last_client_hash: bytes = hmac.new(mac_key, data, self.hashfunc).digest()
        data += self.last_client_hash[:2]
        self.pack_id = (self.pack_id + 1) & 0xFFFFFFFF
        return data

    def pack_server_data(self, buf: bytes) -> bytes:
        """

        pack_data=
        |ushort(len(origin data)^ushort(last_client_hash[14:16]))|encrypt(origin data)|

        HMAC_key=
        |user_key|uint(last_pack_id)|

        next_client_hash=
        HMAC_sign(key:(HMAC_key), be sign data:(pack_data))

        next_pack_id=
        uint((pack_id + 1)&0xFFFFFFFF)

        pack_out=
        |(pack_data)|next_client_hash|


        :param buf: bytes   origin data
        :return: bytes      packed data
        """
        buf = self.encryptor.encrypt(buf)
        data = self.rnd_data(len(buf), buf, self.last_server_hash, self.random_server)
        mac_key = self.user_key + struct.pack('<I', self.pack_id)
        length = len(buf) ^ struct.unpack('<H', self.last_server_hash[14:])[0]
        data = struct.pack('<H', length) + data
        self.last_server_hash = hmac.new(mac_key, data, self.hashfunc).digest()
        data += self.last_server_hash[:2]
        if self.pack_id == 1:
            self.send_tcp_mss = self.new_send_tcp_mss
        self.pack_id = (self.pack_id + 1) & 0xFFFFFFFF
        return data

    def pack_auth_data(self, auth_data: bytes, buf: bytes) -> bytes:
        """
        ### construct the client first&auth pack

        plain_auth_data=
        |auth_data|ushort(server_info.overhead)|ushort(tcp_mss)|

        check_head 1st_pair=
        |rand_bytes(4)|

        mac_key=
        |server_info.iv|server_info.key|

        client_hash=
        |HMAC_sign(mac_key, data:(check_head 1st_pair))|

        check_head 2ed_pair=
        |client_hash[:8]|

        check_head=
        |(check_head 1st_pair)|(check_head 2ed_pair)|

        user_origin_id=
        |(uid in protocol_param)| or |rand_bytes(4)|

        user_encoded_id=
        |uint_bytes(uint_number(uid)^uint_number(client_hash[8:12]))|
        {== equal as ==|uint_bytes(uid)^client_hash[8:12]|}

        encryptor_1_key=
        |bytes(base64(user_key))|protocol_salt|

        encrypted_auth_data=
        |user_encoded_id|
        |encryptor(
            key:(encryptor_1_key),
            data:(plain_auth_data),
            mode:('aes-128-cbc'),
            encipher_vi:(b'\x00' * 16)
        )[16:]|

        server_hash=
        |HMAC_sign(user_key, encrypted_auth_data)|

        pack_out=
        |check_head|encrypted_auth_data|server_hash[:4]|pack_client_data(buf)|

        connect_encryptor_key=
        |bytes(base64(user_key))|bytes(base64(client_hash[0:16]))|

        connect_encryptor_encipher_vi=
        |client_hash[:8]|

        connect_encryptor_decipher_vi=
        |server_hash[:8]|

        connect_encryptor=
        |encryptor(
            key:(connect_encryptor_key),
            data:(plain_auth_data),
            mode:('chacha20'),
            encipher_vi:(connect_encryptor_encipher_vi),
            decipher_vi:(connect_encryptor_decipher_vi)
        )|

        :param auth_data:
        :param buf:
        :return:
        """
        data = auth_data
        # TODO FIX THIS   this need be a little ending encode ushort ,
        # TODO it need same as decoder on `server_post_decrypt()` for recv_tcp_mss
        # a random size tcp max size for mss exchange when c->s
        self.send_tcp_mss = struct.unpack('>H', rand_bytes(2))[0] % 1024 + 400
        data = data + (struct.pack('<H', self.server_info.overhead) + struct.pack('<H', self.send_tcp_mss))
        mac_key = self.server_info.iv + self.server_info.key

        check_head = rand_bytes(4)
        self.last_client_hash = hmac.new(mac_key, check_head, self.hashfunc).digest()
        check_head += self.last_client_hash[:8]

        # TODO FIX THIS   generate default user id before `if`
        # if exist, get the user origin_id/key from protocol_param
        # otherwise, use `rand_bytes(4)` as default user origin_id, the global protocol key(password) as user key
        if b':' in to_bytes(self.server_info.protocol_param):
            try:
                items = to_bytes(self.server_info.protocol_param).split(b':')
                self.user_key = items[1]
                uid: bytes = struct.pack('<I', int(items[0]))
            except:
                uid: bytes = rand_bytes(4)
        else:
            uid: bytes = rand_bytes(4)
        if self.user_key is None:
            self.user_key = self.server_info.key

        # use user key to initial a aes-128-cbc encryptor, vi is 16-bytes 0x00
        # and use it to encrypt plain auth data
        encryptor = encrypt.Encryptor(
            to_bytes(base64.b64encode(self.user_key)) + self.salt, 'aes-128-cbc', b'\x00' * 16)

        uid = struct.unpack('<I', uid)[0] ^ struct.unpack('<I', self.last_client_hash[8:12])[0]
        uid = struct.pack('<I', uid)
        data = uid + encryptor.encrypt(data)[16:]

        self.last_server_hash = hmac.new(self.user_key, data, self.hashfunc).digest()
        data = check_head + data + self.last_server_hash[:4]

        # use user_key and client_hash to initial a chacha20 encryptor as connect encryptor, vi is client_hash[:8]
        self.encryptor = encrypt.Encryptor(
            to_bytes(base64.b64encode(self.user_key)) + to_bytes(base64.b64encode(self.last_client_hash)), 'chacha20',
            self.last_client_hash[:8])
        # set encryptor's iv_sent state to True, to make it not place vi at front of first encrypted pack
        # *** because the receive point can re-construct encipher.vi from client_hash[:8]
        #     client_hash[:8] are the check_head[4:8]
        #     client_hash[0:16] are calc from `HMAC(mac_key, check_head[0:4])`
        #     mac_key=server.vi+server.key
        # so , we don't need send encryptor's encipher's vi
        self.encryptor.encrypt(b'')
        # use server_hash[:8] as encryptor's decipher's iv to init encryptor's decipher
        # *** because the receive point can re-construct decipher.vi from server_hash[:8]
        #     server_hash[0:16] can calc from `HMAC(user_key, encrypted_auth_data)`
        #     server_hash[:4] are the verifier for the `user_key` and `encrypted_auth_data`,
        #     it place back at encrypted_auth_data
        # same as above , we dont need send decipher's vi
        self.encryptor.decrypt(self.last_server_hash[:8])
        return data + self.pack_client_data(buf)

    def auth_data(self) -> bytes:
        """

        auth_data=
        |uint(utc_time)|connection_id:(rand_bytes(4))|uint(connection_id)|

        :return: bytes    auth data
        """
        utc_time: int = int(time.time()) & 0xFFFFFFFF
        if self.server_info.data.connection_id > 0xFF000000:
            self.server_info.data.local_client_id = b''
        if not self.server_info.data.local_client_id:
            self.server_info.data.local_client_id = rand_bytes(4)
            logging.debug("local_client_id %s" % (binascii.hexlify(self.server_info.data.local_client_id),))
            self.server_info.data.connection_id = struct.unpack('<I', rand_bytes(4))[0] & 0xFFFFFF
        self.server_info.data.connection_id += 1
        return b''.join([struct.pack('<I', utc_time),
                         self.server_info.data.local_client_id,
                         struct.pack('<I', self.server_info.data.connection_id)])

    def on_recv_auth_data(self, utc_time):
        # TODO implement
        pass

    def client_pre_encrypt(self, buf: bytes) -> bytes:
        """

        header=
        |packed data(auth_data)|

        ret_data=
        |header:(if first pack)|packed data(buf)|

        :param buf: bytes    plain data
        :return: bytes     encrypted data
        """
        ret = b''
        ogn_data_len = len(buf)
        if not self.has_sent_header:
            head_size = self.get_head_size(buf, 30)
            datalen = min(len(buf), random.randint(0, 31) + head_size)
            ret += self.pack_auth_data(self.auth_data(), buf[:datalen])  # TODO buf[:datalen] may overflow
            buf = buf[datalen:]
            self.has_sent_header = True
        while len(buf) > self.unit_len:
            ret += self.pack_client_data(buf[:self.unit_len])
            buf = buf[self.unit_len:]
        ret += self.pack_client_data(buf)
        return ret

    def client_post_decrypt(self, buf):
        if self.raw_trans:
            return buf
        self.recv_buf += buf
        out_buf = b''
        while len(self.recv_buf) > 4:
            mac_key = self.user_key + struct.pack('<I', self.recv_id)
            data_len = struct.unpack('<H', self.recv_buf[:2])[0] ^ struct.unpack('<H', self.last_server_hash[14:16])[0]
            rand_len = self.recv_rnd_data_len(data_len, self.last_server_hash, self.random_server)
            length = data_len + rand_len
            if length >= 4096:
                self.raw_trans = True
                self.recv_buf = b''
                raise Exception('client_post_decrypt data error')

            if length + 4 > len(self.recv_buf):
                break

            server_hash = hmac.new(mac_key, self.recv_buf[:length + 2], self.hashfunc).digest()
            if server_hash[:2] != self.recv_buf[length + 2: length + 4]:
                logging.info('%s: checksum error, data %s'
                             % (self.no_compatible_method, binascii.hexlify(self.recv_buf[:length])))
                self.raw_trans = True
                self.recv_buf = b''
                raise Exception('client_post_decrypt data uncorrect checksum')

            pos = 2
            if data_len > 0 and rand_len > 0:
                pos = 2
            out_buf += self.encryptor.decrypt(self.recv_buf[pos: data_len + pos])
            self.last_server_hash = server_hash
            if self.recv_id == 1:
                self.server_info.tcp_mss = struct.unpack('<H', out_buf[:2])[0]
                self.recv_tcp_mss = self.server_info.tcp_mss
                self.send_back_cmd.append(0xff00)
                out_buf = out_buf[2:]
            self.recv_id = (self.recv_id + 1) & 0xFFFFFFFF
            self.recv_buf = self.recv_buf[length + 4:]

        return out_buf

    def server_pre_encrypt(self, buf):
        if self.raw_trans:
            return buf
        ret = b''
        if self.pack_id == 1:
            tcp_mss = self.server_info.tcp_mss if self.server_info.tcp_mss < 1500 else 1500
            self.server_info.tcp_mss = tcp_mss
            buf = struct.pack('<H', tcp_mss) + buf
            self.unit_len = tcp_mss - self.client_over_head
            self.new_send_tcp_mss = tcp_mss
        while len(buf) > self.unit_len:
            ret += self.pack_server_data(buf[:self.unit_len])
            buf = buf[self.unit_len:]
        ret += self.pack_server_data(buf)
        return ret

    def server_post_decrypt(self, buf):
        if self.raw_trans:
            return (buf, False)
        self.recv_buf += buf
        out_buf = b''
        sendback = False

        if not self.has_recv_header:
            if len(self.recv_buf) >= 12 or len(self.recv_buf) in [7, 8]:
                recv_len = min(len(self.recv_buf), 12)
                mac_key = self.server_info.recv_iv + self.server_info.key
                md5data = hmac.new(mac_key, self.recv_buf[:4], self.hashfunc).digest()
                if md5data[:recv_len - 4] != self.recv_buf[4:recv_len]:
                    return self.not_match_return(self.recv_buf)

            if len(self.recv_buf) < 12 + 24:
                return (b'', False)

            self.last_client_hash = md5data
            uid = struct.unpack('<I', self.recv_buf[12:16])[0] ^ struct.unpack('<I', md5data[8:12])[0]
            self.user_id_num = uid
            uid = struct.pack('<I', uid)
            if uid in self.server_info.users:
                self.user_id = uid
                self.user_key = self.server_info.users[uid]
                self.server_info.update_user_func(uid)
            else:
                self.user_id_num = 0
                if not self.server_info.users:
                    self.user_key = self.server_info.key
                else:
                    self.user_key = self.server_info.recv_iv

            md5data = hmac.new(self.user_key, self.recv_buf[12: 12 + 20], self.hashfunc).digest()
            if md5data[:4] != self.recv_buf[32:36]:
                logging.error('%s data uncorrect auth HMAC-MD5 from %s:%d, data %s' % (
                    self.no_compatible_method, self.server_info.client, self.server_info.client_port,
                    binascii.hexlify(self.recv_buf)
                ))
                if len(self.recv_buf) < 36:
                    return (b'', False)
                return self.not_match_return(self.recv_buf)

            self.last_server_hash = md5data
            encryptor = encrypt.Encryptor(to_bytes(base64.b64encode(self.user_key)) + self.salt, 'aes-128-cbc')
            head = encryptor.decrypt(b'\x00' * 16 + self.recv_buf[16:32] + b'\x00')  # need an extra byte or recv empty
            self.client_over_head = struct.unpack('<H', head[12:14])[0]
            self.recv_tcp_mss = struct.unpack('<H', head[14:16])[0]
            self.send_tcp_mss = self.recv_tcp_mss

            utc_time = struct.unpack('<I', head[:4])[0]
            client_id = struct.unpack('<I', head[4:8])[0]
            connection_id = struct.unpack('<I', head[8:12])[0]
            time_dif = common.int32(utc_time - (int(time.time()) & 0xffffffff))
            if time_dif < -self.max_time_dif or time_dif > self.max_time_dif:
                logging.info('%s: wrong timestamp, time_dif %d, data %s' % (
                    self.no_compatible_method, time_dif, binascii.hexlify(head)
                ))
                return self.not_match_return(self.recv_buf)
            elif self.server_info.data.insert(self.user_id, client_id, connection_id):
                self.has_recv_header = True
                self.client_id = client_id
                self.connection_id = connection_id
            else:
                logging.info('%s: auth fail, data %s' % (self.no_compatible_method, binascii.hexlify(out_buf)))
                return self.not_match_return(self.recv_buf)

            self.on_recv_auth_data(utc_time)
            self.encryptor = encrypt.Encryptor(
                to_bytes(base64.b64encode(self.user_key)) + to_bytes(base64.b64encode(self.last_client_hash)),
                'chacha20', self.last_server_hash[:8])
            self.encryptor.encrypt(b'')
            self.encryptor.decrypt(self.last_client_hash[:8])
            self.recv_buf = self.recv_buf[36:]
            self.has_recv_header = True
            sendback = True

        while len(self.recv_buf) > 4:
            mac_key = self.user_key + struct.pack('<I', self.recv_id)
            recv_buf = self.recv_buf
            data_len = struct.unpack('<H', recv_buf[:2])[0] ^ struct.unpack('<H', self.last_client_hash[14:16])[0]
            cmd_len = 0
            while data_len >= 0xff00:
                if data_len == 0xff00:
                    cmd_len += 2
                    self.recv_tcp_mss = self.send_tcp_mss
                    recv_buf = recv_buf[2:]
                    data_len = struct.unpack('<H', recv_buf[:2])[0] ^ struct.unpack('<H', self.last_client_hash[12:14])[
                        0]
                else:
                    self.raw_trans = True
                    self.recv_buf = b''
                    if self.recv_id == 1:
                        logging.info(self.no_compatible_method + ': over size')
                        return (b'E' * 2048, False)
                    else:
                        raise Exception('server_post_decrype data error')
            rand_len = self.recv_rnd_data_len(data_len + cmd_len, self.last_client_hash, self.random_client)
            length = data_len + rand_len
            if length >= 4096:
                self.raw_trans = True
                self.recv_buf = b''
                if self.recv_id == 1:
                    logging.info(self.no_compatible_method + ': over size')
                    return (b'E' * 2048, False)
                else:
                    raise Exception('server_post_decrype data error')

            if length + 4 > len(recv_buf):
                break

            client_hash = hmac.new(mac_key, self.recv_buf[:length + cmd_len + 2], self.hashfunc).digest()
            if client_hash[:2] != self.recv_buf[length + cmd_len + 2: length + cmd_len + 4]:
                logging.info('%s: checksum error, data %s' % (
                    self.no_compatible_method, binascii.hexlify(self.recv_buf[:length + cmd_len]),
                ))
                self.raw_trans = True
                self.recv_buf = b''
                if self.recv_id == 1:
                    return (b'E' * 2048, False)
                else:
                    raise Exception('server_post_decrype data uncorrect checksum')

            self.recv_id = (self.recv_id + 1) & 0xFFFFFFFF
            pos = 2
            if data_len > 0 and rand_len > 0:
                pos = 2
            out_buf += self.encryptor.decrypt(recv_buf[pos: data_len + pos])
            self.last_client_hash = client_hash
            self.recv_buf = recv_buf[length + 4:]
            if data_len == 0:
                sendback = True

        if out_buf:
            self.server_info.data.update(self.user_id, self.client_id, self.connection_id)
        return (out_buf, sendback)

    def client_udp_pre_encrypt(self, buf):
        if self.user_key is None:
            if b':' in to_bytes(self.server_info.protocol_param):
                try:
                    items = to_bytes(self.server_info.protocol_param).split(':')
                    self.user_key = self.hashfunc(items[1]).digest()
                    self.user_id = struct.pack('<I', int(items[0]))
                except:
                    pass
            if self.user_key is None:
                self.user_id = rand_bytes(4)
                self.user_key = self.server_info.key
        authdata = rand_bytes(3)
        mac_key = self.server_info.key
        md5data = hmac.new(mac_key, authdata, self.hashfunc).digest()
        uid = struct.unpack('<I', self.user_id)[0] ^ struct.unpack('<I', md5data[:4])[0]
        uid = struct.pack('<I', uid)
        rand_len = self.udp_rnd_data_len(md5data, self.random_client)
        encryptor = encrypt.Encryptor(
            to_bytes(base64.b64encode(self.user_key)) + to_bytes(base64.b64encode(md5data)), 'chacha20', mac_key[:8])
        encryptor.encrypt(b'')
        out_buf = encryptor.encrypt(buf)
        buf = out_buf + rand_bytes(rand_len) + authdata + uid
        return buf + hmac.new(self.user_key, buf, self.hashfunc).digest()[:1]

    def client_udp_post_decrypt(self, buf):
        if len(buf) <= 8:
            return (b'', None)
        if hmac.new(self.user_key, buf[:-1], self.hashfunc).digest()[:1] != buf[-1:]:
            return (b'', None)
        mac_key = self.server_info.key
        md5data = hmac.new(mac_key, buf[-8:-1], self.hashfunc).digest()
        rand_len = self.udp_rnd_data_len(md5data, self.random_server)
        encryptor = encrypt.Encryptor(
            to_bytes(base64.b64encode(self.user_key)) + to_bytes(base64.b64encode(md5data)), 'chacha20')
        encryptor.decrypt(mac_key[:8])
        return encryptor.decrypt(buf[:-8 - rand_len])

    def server_udp_pre_encrypt(self, buf, uid):
        if uid in self.server_info.users:
            user_key = self.server_info.users[uid]
        else:
            uid = None
            if not self.server_info.users:
                user_key = self.server_info.key
            else:
                user_key = self.server_info.recv_iv
        authdata = rand_bytes(7)
        mac_key = self.server_info.key
        md5data = hmac.new(mac_key, authdata, self.hashfunc).digest()
        rand_len = self.udp_rnd_data_len(md5data, self.random_server)
        encryptor = encrypt.Encryptor(to_bytes(base64.b64encode(user_key)) + to_bytes(base64.b64encode(md5data)),
                                      'chacha20', mac_key[:8])
        encryptor.encrypt(b'')
        out_buf = encryptor.encrypt(buf)
        buf = out_buf + rand_bytes(rand_len) + authdata
        return buf + hmac.new(user_key, buf, self.hashfunc).digest()[:1]

    def server_udp_post_decrypt(self, buf):
        mac_key = self.server_info.key
        md5data = hmac.new(mac_key, buf[-8:-5], self.hashfunc).digest()
        uid = struct.unpack('<I', buf[-5:-1])[0] ^ struct.unpack('<I', md5data[:4])[0]
        uid = struct.pack('<I', uid)
        if uid in self.server_info.users:
            user_key = self.server_info.users[uid]
        else:
            uid = None
            if not self.server_info.users:
                user_key = self.server_info.key
            else:
                user_key = self.server_info.recv_iv
        if hmac.new(user_key, buf[:-1], self.hashfunc).digest()[:1] != buf[-1:]:
            return (b'', None)
        rand_len = self.udp_rnd_data_len(md5data, self.random_client)
        encryptor = encrypt.Encryptor(to_bytes(base64.b64encode(user_key)) + to_bytes(base64.b64encode(md5data)),
                                      'chacha20')
        encryptor.decrypt(mac_key[:8])
        out_buf = encryptor.decrypt(buf[:-8 - rand_len])
        return (out_buf, uid)

    def dispose(self):
        self.server_info.data.remove(self.user_id, self.client_id)


class auth_akarin_spec_a(auth_akarin_rand):
    def __init__(self, method):
        super(auth_akarin_spec_a, self).__init__(method)
        self.salt = b"auth_akarin_spec_a"
        self.no_compatible_method = 'auth_akarin_spec_a'
        self.data_size_list = []
        self.data_size_list2 = []

    def init_data_size(self, key):
        if self.data_size_list:
            self.data_size_list = []
            self.data_size_list2 = []
        random = xorshift128plus()
        random.init_from_bin(key)
        list_len = random.next() % 8 + 4
        for i in range(0, list_len):
            self.data_size_list.append((int)(random.next() % 2340 % 2040 % 1440))
        self.data_size_list.sort()
        list_len = random.next() % 16 + 8
        for i in range(0, list_len):
            self.data_size_list2.append((int)(random.next() % 2340 % 2040 % 1440))
        self.data_size_list2.sort()

    def set_server_info(self, server_info):
        self.server_info = server_info
        try:
            max_client = int(server_info.protocol_param.split('#')[0])
        except:
            max_client = 64
        self.server_info.data.set_max_client(max_client)
        self.init_data_size(self.server_info.key)

    def send_rnd_data_len(self, buf_size, last_hash, random):
        if buf_size + self.server_info.overhead > self.send_tcp_mss:
            random.init_from_bin_len(last_hash, buf_size)
            return random.next() % 521
        if buf_size >= 1440 or buf_size + self.server_info.overhead == self.send_tcp_mss:
            return 0
        random.init_from_bin_len(last_hash, buf_size)
        pos = bisect.bisect_left(self.data_size_list, buf_size + self.server_info.overhead)
        final_pos = pos + random.next() % (len(self.data_size_list))
        if final_pos < len(self.data_size_list):
            return self.data_size_list[final_pos] - buf_size - self.server_info.overhead

        pos = bisect.bisect_left(self.data_size_list2, buf_size + self.server_info.overhead)
        final_pos = pos + random.next() % (len(self.data_size_list2))
        if final_pos < len(self.data_size_list2):
            return self.data_size_list2[final_pos] - buf_size - self.server_info.overhead
        if final_pos < pos + len(self.data_size_list2) - 1:
            return 0

        if buf_size > 1300:
            return random.next() % 31
        if buf_size > 900:
            return random.next() % 127
        if buf_size > 400:
            return random.next() % 521
        return random.next() % 1021

    def recv_rnd_data_len(self, buf_size, last_hash, random):
        if buf_size + self.server_info.overhead > self.recv_tcp_mss:
            random.init_from_bin_len(last_hash, buf_size)
            return random.next() % 521
        if buf_size >= 1440 or buf_size + self.server_info.overhead == self.send_tcp_mss:
            return 0
        random.init_from_bin_len(last_hash, buf_size)
        pos = bisect.bisect_left(self.data_size_list, buf_size + self.server_info.overhead)
        final_pos = pos + random.next() % (len(self.data_size_list))
        if final_pos < len(self.data_size_list):
            return self.data_size_list[final_pos] - buf_size - self.server_info.overhead

        pos = bisect.bisect_left(self.data_size_list2, buf_size + self.server_info.overhead)
        final_pos = pos + random.next() % (len(self.data_size_list2))
        if final_pos < len(self.data_size_list2):
            return self.data_size_list2[final_pos] - buf_size - self.server_info.overhead
        if final_pos < pos + len(self.data_size_list2) - 1:
            return 0

        if buf_size > 1300:
            return random.next() % 31
        if buf_size > 900:
            return random.next() % 127
        if buf_size > 400:
            return random.next() % 521
        return random.next() % 1021
