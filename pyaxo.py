"""
pyaxo.py - a python implementation of the axolotl ratchet protocol.
https://github.com/trevp/axolotl/wiki/newversion

Symmetric encryption is done using the python-gnupg module.

Copyright (C) 2014 by David R. Andersen <k0rx@RXcomm.net>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.

For more information, see https://github.com/rxcomm/pyaxo
"""

import errno
import sqlite3
import binascii
import hmac
import os
import sys
import nacl.utils
import nacl.secret
from binascii import a2b_base64 as a2b
from binascii import b2a_base64 as b2a
from getpass import getpass
from time import time
from threading import Lock
from passlib.utils.pbkdf2 import pbkdf2
from nacl.public import PrivateKey, PublicKey, Box
from nacl.exceptions import CryptoError
from nacl.hash import sha256

ALICE_MODE = True
BOB_MODE = False

SALTS = {'RK': b'\x00',
         'HK': {ALICE_MODE: b'\x01', BOB_MODE: b'\x02'},
         'NHK': {ALICE_MODE: b'\x03', BOB_MODE: b'\x04'},
         'CK': {ALICE_MODE: b'\x05', BOB_MODE: b'\x06'},
         'CONVid': b'\x07'}

HEADER_LEN = 80
HEADER_PAD_NUM_LEN = 1
HEADER_COUNT_NUM_LEN = 3

class Axolotl(object):

    def __init__(self, name, dbname='axolotl.db', dbpassphrase='', nonthreaded_sql=True):
        self.name = name
        self.dbname = dbname
        self.nonthreaded_sql = nonthreaded_sql
        if dbpassphrase is None:
            self.dbpassphrase = None
        elif dbpassphrase != '':
            self.dbpassphrase = hash_(dbpassphrase)
        else:
            self.dbpassphrase = getpass('Database passphrase for '+ self.name + ': ').strip()
        self.conversation = AxolotlConversation(self, state=dict(), mode=None)
        self.state['DHIs_priv'], self.state['DHIs'] = generate_keypair()
        self.state['DHRs_priv'], self.state['DHRs'] = generate_keypair()
        self.handshakeKey, self.handshakePKey = generate_keypair()
        self.storeTime = 2*86400 # minimum time (seconds) to store missed ephemeral message keys
        self.persistence = SqlitePersistence(self.dbname,
                                             self.dbpassphrase,
                                             self.storeTime,
                                             self.nonthreaded_sql)

        self.commit_skipped_mk(self.conversation)

    @property
    def state(self):
        return self.conversation.state

    @state.setter
    def state(self, state):
        self.conversation.state = state

    @property
    def mode(self):
        return self.conversation.mode

    @mode.setter
    def mode(self, mode):
        self.conversation.mode = mode

    @property
    def db(self):
        return self.persistence.db

    @db.setter
    def db(self, db):
        self.persistence.db = db

    def tripleDH(self, a, a0, B, B0):
        if self.mode == None:
            sys.exit(1)
        return generate_3dh(a, a0, B, B0, self.mode)

    def genDH(self, a, B):
        return generate_dh(a, B)

    def genKey(self):
        return generate_keypair()

    def initState(self, other_name, other_identityKey, other_handshakeKey,
                  other_ratchetKey, verify=True):
        if verify:
            print 'Confirm ' + other_name + ' has identity key fingerprint:\n'
            fingerprint = hash_(other_identityKey).encode('hex').upper()
            fprint = ''
            for i in range(0, len(fingerprint), 4):
                fprint += fingerprint[i:i+2] + ':'
            print fprint[:-1] + '\n'
            print 'Be sure to verify this fingerprint with ' + other_name + \
                  ' by some out-of-band method!'
            print 'Otherwise, you may be subject to a Man-in-the-middle attack!\n'
            ans = raw_input('Confirm? y/N: ').strip()
            if ans != 'y':
                print 'Key fingerprint not confirmed - exiting...'
                sys.exit()

        self.conversation = self.init_conversation(other_name,
                                                   self.state['DHIs_priv'],
                                                   self.state['DHIs'],
                                                   self.handshakeKey,
                                                   other_identityKey,
                                                   other_handshakeKey,
                                                   self.state['DHRs_priv'],
                                                   self.state['DHRs'],
                                                   other_ratchetKey)

    def init_conversation(self, other_name,
                          priv_identity_key, identity_key, priv_handshake_key,
                          other_identity_key, other_handshake_key,
                          priv_ratchet_key=None, ratchet_key=None,
                          other_ratchet_key=None, mode=None):
        if mode is None:
            if identity_key < other_identity_key:
                mode = ALICE_MODE
            else:
                mode = BOB_MODE

        mkey = generate_3dh(priv_identity_key, priv_handshake_key,
                            other_identity_key, other_handshake_key,
                            mode)

        return self.create_conversation(other_name,
                                        mkey,
                                        mode,
                                        priv_identity_key,
                                        identity_key,
                                        other_identity_key,
                                        priv_ratchet_key,
                                        ratchet_key,
                                        other_ratchet_key)

    def createState(self, other_name, mkey, mode=None, other_identityKey=None, other_ratchetKey=None):
        if mode is not None:
            self.mode = mode
        else:
            if self.mode is None: # mode not selected
                sys.exit(1)

        self.conversation = self.create_conversation(other_name,
                                                     mkey,
                                                     self.mode,
                                                     self.state['DHIs_priv'],
                                                     self.state['DHIs'],
                                                     other_identityKey,
                                                     self.state['DHRs_priv'],
                                                     self.state['DHRs'],
                                                     other_ratchetKey)

        self.ratchetKey = False
        self.ratchetPKey = False

    def create_conversation(self, other_name, mkey, mode,
                            priv_identity_key, identity_key,
                            other_identity_key,
                            priv_ratchet_key=None, ratchet_key=None,
                            other_ratchet_key=None):
        if mode is ALICE_MODE:
            HKs = None
            HKr = kdf(mkey, SALTS['HK'][BOB_MODE])
            CKs = None
            CKr = kdf(mkey, SALTS['CK'][BOB_MODE])
            DHRs_priv = None
            DHRs = None
            DHRr = other_ratchet_key
            Ns = 0
            Nr = 0
            PNs = 0
            ratchet_flag = True
        else: # bob mode
            HKs = kdf(mkey, SALTS['HK'][BOB_MODE])
            HKr = None
            CKs = kdf(mkey, SALTS['CK'][BOB_MODE])
            CKr = None
            DHRs_priv = priv_ratchet_key
            DHRs = ratchet_key
            DHRr = None
            Ns = 0
            Nr = 0
            PNs = 0
            ratchet_flag = False
        RK = kdf(mkey, SALTS['RK'])
        NHKs = kdf(mkey, SALTS['NHK'][mode])
        NHKr = kdf(mkey, SALTS['NHK'][not mode])
        CONVid = kdf(mkey, SALTS['CONVid'])
        DHIr = other_identity_key

        state = \
               { 'name': self.name,
                 'other_name': other_name,
                 'RK': RK,
                 'HKs': HKs,
                 'HKr': HKr,
                 'NHKs': NHKs,
                 'NHKr': NHKr,
                 'CKs': CKs,
                 'CKr': CKr,
                 'DHIs_priv': priv_identity_key,
                 'DHIs': identity_key,
                 'DHIr': DHIr,
                 'DHRs_priv': DHRs_priv,
                 'DHRs': DHRs,
                 'DHRr': DHRr,
                 'CONVid': CONVid,
                 'Ns': Ns,
                 'Nr': Nr,
                 'PNs': PNs,
                 'ratchet_flag': ratchet_flag,
               }

        return AxolotlConversation(self, state, mode)

    def encrypt(self, plaintext):
        return self.conversation.encrypt(plaintext)

    def enc(self, key, plaintext):
        return encrypt_symmetric(key, plaintext)

    def dec(self, key, encrypted):
        return decrypt_symmetric(key, encrypted)

    def commit_skipped_mk(self, conversation):
        self.persistence.commit_skipped_mk(conversation.staged_hk_mk,
                                           conversation.state)

    def try_skipped_mk(self, msg, pad_length, conversation):
        return self.persistence.try_skipped_mk(
            msg, pad_length,
            conversation.state['name'], conversation.state['other_name'])

    def decrypt(self, msg):
        return self.conversation.decrypt(msg)

    def encrypt_file(self, filename):
        self.conversation.encrypt_file(filename)

    def decrypt_file(self, filename):
        self.conversation.decrypt_file(filename)

    def encrypt_pipe(self):
        self.conversation.encrypt_pipe()

    def decrypt_pipe(self):
        self.conversation.decrypt_pipe()

    def printKeys(self):
        self.conversation.print_keys()

    def saveState(self):
        self.save_conversation(self.conversation)

    def save_conversation(self, conversation):
        self.persistence.save_state(conversation.state, conversation.mode)

    def loadState(self, name, other_name):
        self.persistence.db = self.openDB()
        self.conversation = self.load_conversation(name, other_name)
        if self.conversation:
            return
        else:
            return False

    def load_conversation(self, name, other_name):
        state, mode = self.persistence.load_state(name, other_name)
        return AxolotlConversation(self, state, mode)

    def openDB(self):
        return self.persistence._open_db()

    def writeDB(self):
        self.persistence.write_db()

    def printState(self):
        self.conversation.print_state()


class AxolotlConversation:
    def __init__(self, axolotl, state, mode):
        self._axolotl = axolotl
        self.state = state
        self.mode = mode
        self.staged_hk_mk = dict()

        self.handshake_key = None
        self.handshake_pkey = None

    def encrypt(self, plaintext):
        if self.state['ratchet_flag']:
            self.state['DHRs_priv'], self.state['DHRs'] = generate_keypair()
            self.state['HKs'] = self.state['NHKs']
            self.state['RK'] = hash_(self.state['RK'] +
                                     generate_dh(self.state['DHRs_priv'], self.state['DHRr']))
            self.state['NHKs'] = kdf(self.state['RK'], SALTS['NHK'][self.mode])
            self.state['CKs'] = kdf(self.state['RK'], SALTS['CK'][self.mode])
            self.state['PNs'] = self.state['Ns']
            self.state['Ns'] = 0
            self.state['ratchet_flag'] = False
        mk = hash_(self.state['CKs'] + '0')
        msg1 = encrypt_symmetric(
            self.state['HKs'],
            str(self.state['Ns']).zfill(HEADER_COUNT_NUM_LEN) +
            str(self.state['PNs']).zfill(HEADER_COUNT_NUM_LEN) +
            self.state['DHRs'])
        msg2 = encrypt_symmetric(mk, plaintext)
        pad_length = HEADER_LEN - len(msg1)
        pad = os.urandom(pad_length - HEADER_PAD_NUM_LEN) + chr(pad_length)
        msg = msg1 + pad + msg2
        self.state['Ns'] += 1
        self.state['CKs'] = hash_(self.state['CKs'] + '1')
        return msg

    def decrypt(self, msg):
        pad = msg[HEADER_LEN-HEADER_PAD_NUM_LEN:HEADER_LEN]
        pad_length = ord(pad)
        msg1 = msg[:HEADER_LEN-pad_length]

        body = self.try_skipped_mk(msg, pad_length)
        if body and body != '':
            return body

        header = None
        if self.state['HKr']:
            try:
                header = decrypt_symmetric(self.state['HKr'], msg1)
            except CryptoError:
                pass
        if header and header != '':
            Np = int(header[:HEADER_COUNT_NUM_LEN])
            CKp, mk = self.stage_skipped_mk(self.state['HKr'], self.state['Nr'], Np, self.state['CKr'])
            try:
                body = decrypt_symmetric(mk, msg[HEADER_LEN:])
            except CryptoError:
                print 'Undecipherable message'
                sys.exit(1)
        else:
            try:
                header = decrypt_symmetric(self.state['NHKr'], msg1)
            except CryptoError:
                pass
            if self.state['ratchet_flag'] or not header or header == '':
                print 'Undecipherable message'
                sys.exit(1)
            Np = int(header[:HEADER_COUNT_NUM_LEN])
            PNp = int(header[HEADER_COUNT_NUM_LEN:HEADER_COUNT_NUM_LEN*2])
            DHRp = header[HEADER_COUNT_NUM_LEN*2:]
            if self.state['CKr']:
                self.stage_skipped_mk(self.state['HKr'], self.state['Nr'], PNp, self.state['CKr'])
            HKp = self.state['NHKr']
            RKp = hash_(self.state['RK'] + generate_dh(self.state['DHRs_priv'], DHRp))
            NHKp = kdf(RKp, SALTS['NHK'][not self.mode])
            CKp = kdf(RKp, SALTS['CK'][not self.mode])
            CKp, mk = self.stage_skipped_mk(HKp, 0, Np, CKp)
            try:
                body = decrypt_symmetric(mk, msg[HEADER_LEN:])
            except CryptoError:
                pass
            if not body or body == '':
                print 'Undecipherable message'
                sys.exit(1)
            self.state['RK'] = RKp
            self.state['HKr'] = HKp
            self.state['NHKr'] = NHKp
            self.state['DHRr'] = DHRp
            self.state['DHRs_priv'] = None
            self.state['DHRs'] = None
            self.state['ratchet_flag'] = True
        self.commit_skipped_mk()
        self.state['Nr'] = Np + 1
        self.state['CKr'] = CKp
        return body

    def try_skipped_mk(self, msg, pad_length):
        return self._axolotl.try_skipped_mk(msg, pad_length, self)

    def stage_skipped_mk(self, hkr, nr, np, ckr):
        ckp = ckr
        for i in range(np - nr):
            mk = hash_(ckp + '0')
            ckp = hash_(ckp + '1')
            self.staged_hk_mk[mk] = hkr
        mk = hash_(ckp + '0')
        ckp = hash_(ckp + '1')
        return ckp, mk

    def commit_skipped_mk(self):
        self._axolotl.commit_skipped_mk(self)

    def encrypt_file(self, filename):
        with open(filename, 'r') as f:
            plaintext = f.read()
        ciphertext = b2a(self.encrypt(plaintext))
        with open(filename+'.asc', 'w') as f:
            lines = [ciphertext[i:i+64] for i in xrange(0, len(ciphertext), 64)]
            for line in lines:
                f.write(line+'\n')

    def decrypt_file(self, filename):
        with open(filename, 'r') as f:
            ciphertext = a2b(f.read())
        plaintext = self.decrypt(ciphertext)
        print plaintext

    def encrypt_pipe(self):
        plaintext = sys.stdin.read()
        ciphertext = b2a(self.encrypt(plaintext))
        sys.stdout.write(ciphertext)
        sys.stdout.flush()

    def decrypt_pipe(self):
        ciphertext = a2b(sys.stdin.read())
        plaintext = self.decrypt(ciphertext)
        sys.stdout.write(plaintext)
        sys.stdout.flush()

    def save(self):
        self._axolotl.save_conversation(self)

    def print_keys(self):
        print 'Your Identity key is:\n' + b2a(self.state['DHIs'])
        fingerprint = hash_(self.state['DHIs']).encode('hex').upper()
        fprint = ''
        for i in range(0, len(fingerprint), 4):
            fprint += fingerprint[i:i+2] + ':'
        print 'Your identity key fingerprint is: '
        print fprint[:-1] + '\n'
        print 'Your Ratchet key is:\n' + b2a(self.state['DHRs'])
        if self.handshake_key:
            print 'Your Handshake key is:\n' + b2a(self.handshake_pkey)
        else:
            print 'Your Handshake key is not available'

    def print_state(self):
        print
        print 'Warning: saving this data to disk is insecure!'
        print
        for key in sorted(self.state):
             if 'priv' in key:
                 pass
             else:
                 if self.state[key] is None:
                     print key + ': None'
                 elif type(self.state[key]) is bool:
                     if self.state[key]:
                         print key + ': True'
                     else:
                         print key + ': False'
                 elif type(self.state[key]) is str:
                     try:
                         self.state[key].decode('ascii')
                         print key + ': ' + self.state[key]
                     except UnicodeDecodeError:
                         print key + ': ' + b2a(self.state[key]).strip()
                 else:
                     print key + ': ' + str(self.state[key])
        if self.mode is ALICE_MODE:
            print 'Mode: Alice'
        else:
            print 'Mode: Bob'


class SqlitePersistence(object):
    def __init__(self, dbname, dbpassphrase, store_time, nonthreaded):
        super(SqlitePersistence, self).__init__()
        self.dbname = dbname
        self.dbpassphrase = dbpassphrase
        self.store_time = store_time
        self.nonthreaded = nonthreaded

        self.db = self._open_db()

    def _open_db(self):
        if self.nonthreaded:
            factory = sqlite3.Connection
        else:
            factory = SynchronizedSqliteConnection

        db = sqlite3.connect(':memory:', check_same_thread=self.nonthreaded,
                             factory=factory)
        db.row_factory = sqlite3.Row

        with db:
            try:
                if self.dbpassphrase is not None:
                    with open(self.dbname, 'rb') as f:
                        crypt_sql = f.read()
                        try:
                            sql = decrypt_symmetric(self.dbpassphrase,
                                                    crypt_sql)
                        except CryptoError:
                            print 'Bad passphrase!'
                            sys.exit(1)
                        else:
                            db.cursor().executescript(sql)
                else:
                    with open(self.dbname, 'r') as f:
                        sql = f.read()
                        try:
                            db.cursor().executescript(sql)
                        except sqlite3.OperationalError:
                            print 'Bad sql! Password problem - cannot create the database.'
                            sys.exit(1)
            except IOError as e:
                if e.errno == errno.ENOENT:
                    self._create_db(db)
                else:
                    raise
        return db

    def _create_db(self, db):
        db.execute('''
            CREATE TABLE IF NOT EXISTS
                skipped_mk (
                    my_identity,
                    to_identity,
                    HKr TEXT,
                    mk TEXT,
                    timestamp INTEGER)''')
        db.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS
                message_keys
            ON
                skipped_mk (mk)''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS
                conversations (
                    my_identity TEXT,
                    other_identity TEXT,
                    RK TEXT,
                    HKs TEXT,
                    HKr TEXT,
                    NHKs TEXT,
                    NHKr TEXT,
                    CKs TEXT,
                    CKr TEXT,
                    DHIs_priv TEXT,
                    DHIs TEXT,
                    DHIr TEXT,
                    DHRs_priv TEXT,
                    DHRs TEXT,
                    DHRr TEXT,
                    CONVid TEXT,
                    Ns INTEGER,
                    Nr INTEGER,
                    PNs INTEGER,
                    ratchet_flag INTEGER,
                    mode INTEGER)''')
        db.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS
                conversation_route
            ON
                conversations (
                    my_identity,
                    other_identity)''')

    def write_db(self):
        with self.db as db:
            sql = bytes('\n'.join(db.iterdump()))
            if self.dbpassphrase is not None:
                crypt_sql = encrypt_symmetric(self.dbpassphrase, sql)
                with open(self.dbname, 'wb') as f:
                    f.write(crypt_sql)
            else:
                with open(self.dbname, 'w') as f:
                    f.write(sql)

    def commit_skipped_mk(self, staged_hk_mk, state):
        timestamp = int(time())
        with self.db as db:
            for mk, hkr in staged_hk_mk.iteritems():
                db.execute('''
                    REPLACE INTO
                        skipped_mk (
                            my_identity,
                            to_identity,
                            HKr,
                            mk,
                            timestamp)
                    VALUES (?, ?, ?, ?, ?)''', (
                        state['name'],
                        state['other_name'],
                        b2a(hkr).strip(),
                        b2a(mk).strip(),
                        timestamp))
            rowtime = timestamp - self.store_time
            db.execute('''
                DELETE FROM
                    skipped_mk
                WHERE
                    timestamp < ?''', (rowtime,))

    def try_skipped_mk(self, msg, pad_length, name, other_name):
        with self.db as db:
            rows = db.execute('''
                SELECT
                    *
                FROM
                    skipped_mk
                WHERE
                    my_identity = ? AND
                    to_identity = ?''', (name, other_name))
        for row in rows:
            msg1 = msg[:HEADER_LEN-pad_length]
            msg2 = msg[HEADER_LEN:]
            try:
                header = decrypt_symmetric(a2b(row['hkr']), msg1)
                body = decrypt_symmetric(a2b(row['mk']), msg2)
            except CryptoError:
                header = ''
                body = ''
            if header != '' and body != '':
                with self.db as db:
                    db.execute('''
                        DELETE FROM
                            skipped_mk
                        WHERE
                            mk = ?''', (row['mk'],))
                return body
        return None

    def save_state(self, state, mode):
        HKs = 0 if state['HKs'] is None else b2a(state['HKs']).strip()
        HKr = 0 if state['HKr'] is None else b2a(state['HKr']).strip()
        CKs = 0 if state['CKs'] is None else b2a(state['CKs']).strip()
        CKr = 0 if state['CKr'] is None else b2a(state['CKr']).strip()
        DHIr = 0 if state['DHIr'] is None else b2a(state['DHIr']).strip()
        DHRs_priv = 0 if state['DHRs_priv'] is None else b2a(state['DHRs_priv']).strip()
        DHRs = 0 if state['DHRs'] is None else b2a(state['DHRs']).strip()
        DHRr = 0 if state['DHRr'] is None else b2a(state['DHRr']).strip()
        ratchet_flag = 1 if state['ratchet_flag'] else 0
        mode = 1 if mode else 0
        with self.db as db:
            db.execute('''
                REPLACE INTO
                    conversations (
                        my_identity,
                        other_identity,
                        RK,
                        HKS,
                        HKr,
                        NHKs,
                        NHKr,
                        CKs,
                        CKr,
                        DHIs_priv,
                        DHIs,
                        DHIr,
                        DHRs_priv,
                        DHRs,
                        DHRr,
                        CONVid,
                        Ns,
                        Nr,
                        PNs,
                        ratchet_flag,
                        mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?)''', (
                    state['name'],
                    state['other_name'],
                    b2a(state['RK']).strip(),
                    HKs,
                    HKr,
                    b2a(state['NHKs']).strip(),
                    b2a(state['NHKr']).strip(),
                    CKs,
                    CKr,
                    b2a(state['DHIs_priv']).strip(),
                    b2a(state['DHIs']).strip(),
                    DHIr,
                    DHRs_priv,
                    DHRs,
                    DHRr,
                    b2a(state['CONVid']).strip(),
                    state['Ns'],
                    state['Nr'],
                    state['PNs'],
                    ratchet_flag,
                    mode))
        self.write_db()

    def load_state(self, name, other_name):
        with self.db as db:
            cur = db.cursor()
            cur.execute('''
                SELECT
                    *
                FROM
                    conversations
                WHERE
                    my_identity = ? AND
                    other_identity = ?''', (name, other_name))
            row = cur.fetchone()
        if row:
            state = \
                    { 'name': row['my_identity'],
                        'other_name': row['other_identity'],
                        'RK': a2b(row['rk']),
                        'NHKs': a2b(row['nhks']),
                        'NHKr': a2b(row['nhkr']),
                        'DHIs_priv': a2b(row['dhis_priv']),
                        'DHIs': a2b(row['dhis']),
                        'CONVid': a2b(row['convid']),
                        'Ns': row['ns'],
                        'Nr': row['nr'],
                        'PNs': row['pns'],
                    }
            state['HKs'] = None if row['hks'] == '0' else a2b(row['hks'])
            state['HKr'] = None if row['hkr'] == '0' else a2b(row['hkr'])
            state['CKs'] = None if row['cks'] == '0' else a2b(row['cks'])
            state['CKr'] = None if row['ckr'] == '0' else a2b(row['ckr'])
            state['DHIr'] = None if row['dhir'] == '0' else a2b(row['dhir'])
            state['DHRs_priv'] = None if row['dhrs_priv'] == '0' else a2b(row['dhrs_priv'])
            state['DHRs'] = None if row['dhrs'] == '0' else a2b(row['dhrs'])
            state['DHRr'] = None if row['dhrr'] == '0' else a2b(row['dhrr'])
            ratchet_flag = row['ratchet_flag']
            state['ratchet_flag'] = True if ratchet_flag == 1 \
                                                else False
            mode = row['mode']
            mode = True if mode == 1 else False
            return (state, mode)  # exit at first match
        else:
            return ()  # if no matches


class SynchronizedSqliteConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super(SynchronizedSqliteConnection, self).__init__(*args, **kwargs)
        self.lock = Lock()

    def __enter__(self):
        self.lock.acquire()
        return super(SynchronizedSqliteConnection, self).__enter__()

    def __exit__(self, *args, **kwargs):
        super(SynchronizedSqliteConnection, self).__exit__(*args, **kwargs)
        self.lock.release()


def hash_(data):
    return sha256(data).decode('hex')


def kdf(secret, salt):
    return pbkdf2(secret, salt, rounds=10, prf='hmac-sha256')


def generate_keypair():
    privkey = PrivateKey.generate()
    pubkey = privkey.public_key
    return privkey._private_key, pubkey._public_key


def generate_dh(a, b):
    a = PrivateKey(a)
    b = PublicKey(b)
    key = Box(a, b)
    return key._shared_key


def generate_3dh(a, a0, b, b0, mode=ALICE_MODE):
    if mode is ALICE_MODE:
        return hash_(generate_dh(a, b0) +
                     generate_dh(a0, b) +
                     generate_dh(a0, b0))
    else:
        return hash_(generate_dh(a0, b) +
                     generate_dh(a, b0) +
                     generate_dh(a0, b0))


def encrypt_symmetric(key, plaintext):
    nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
    box = nacl.secret.SecretBox(key)
    return nonce + box.encrypt(plaintext, nonce).ciphertext


def decrypt_symmetric(key, ciphertext):
    box = nacl.secret.SecretBox(key)
    return box.decrypt(ciphertext)
