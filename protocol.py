"""
protocol.py

Implements a custom packet protocol for Battleship game communication.
The protocol provides secure communication with the packet structure,
serialization, checksum verification, and AES encryption.

Default configuration provides verbose debug prints - flag can be set to False.
"""

import struct
import binascii
import random
import time
import socket
import os
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

# Debug configuration
PROTOCOL_VERBOSE_DEBUG = True

# Protocol constants
MAGIC_NUMBER = 0x42534850  # "BSHP" in hex

# Packet type definitions
PACKET_TYPE_USERNAME = 1
PACKET_TYPE_GAME_START = 2
PACKET_TYPE_MOVE = 3 
PACKET_TYPE_BOARD_UPDATE = 4
PACKET_TYPE_GAME_END = 5
PACKET_TYPE_ERROR = 6
PACKET_TYPE_DISCONNECT = 7
PACKET_TYPE_RECONNECT = 8
PACKET_TYPE_ACK = 9
PACKET_TYPE_HEARTBEAT = 10
PACKET_TYPE_CHAT = 11

# Packet structure:
# Header (17 bytes):
#   - Magic Number (4 bytes): Protocol identifier
#   - Sequence Number (4 bytes): Packet ordering
#   - Packet Type (1 byte): Message type
#   - Data Length (4 bytes): Length of encrypted data
#   - Checksum (4 bytes): CRC32 for integrity
# Payload:
#   - IV (16 bytes): Initialization vector
#   - Encrypted data (variable length)

HEADER_SIZE = 17
HEADER_FORMAT = ">IIBI"  # unsigned int, unsigned int, unsigned char, unsigned int

# Encryption configuration
AES_KEY_SIZE = 32  # 256-bit key
IV_SIZE = 16       # 128-bit IV for CTR mode
PRE_SHARED_KEY = b'\x00' * AES_KEY_SIZE

# Global sequence tracking
next_sequence_number = 0

def get_next_sequence_number():
    """Returns the next sequence number for packet ordering."""
    global next_sequence_number
    seq = next_sequence_number
    next_sequence_number = (next_sequence_number + 1) % 0xFFFFFFFF
    return seq

def calculate_checksum(data):
    """Calculates CRC32 checksum for data integrity verification."""
    return binascii.crc32(data) & 0xFFFFFFFF

def _encrypt_payload(payload_bytes, key, iv):
    """Encrypts data using AES-CTR mode."""
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(payload_bytes) + encryptor.finalize()

def _decrypt_payload(encrypted_payload_bytes, key, iv):
    """Decrypts data using AES-CTR mode."""
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    return decryptor.update(encrypted_payload_bytes) + decryptor.finalize()

def create_packet(packet_type, payload):
    """Creates an encrypted packet with the specified type and payload."""
    seq_num = get_next_sequence_number()
    payload_bytes = payload.encode() if isinstance(payload, str) else payload
    
    iv = os.urandom(IV_SIZE)
    encrypted_payload = _encrypt_payload(payload_bytes, PRE_SHARED_KEY, iv)
    
    data_length = IV_SIZE + len(encrypted_payload)
    
    header_prefix = struct.pack(HEADER_FORMAT, 
                             MAGIC_NUMBER, 
                             seq_num, 
                             packet_type, 
                             data_length)
    
    data_to_checksum = header_prefix + iv + encrypted_payload
    checksum = calculate_checksum(data_to_checksum)
    
    full_header = header_prefix + struct.pack(">I", checksum)
    
    return full_header + iv + encrypted_payload

def decode_header(header_bytes):
    """Decodes packet header into its components."""
    if len(header_bytes) != HEADER_SIZE:
        raise ValueError(f"Invalid header size: {len(header_bytes)}, expected {HEADER_SIZE}")
    
    magic, seq, ptype, dlen = struct.unpack(HEADER_FORMAT, header_bytes[:13])
    checksum = struct.unpack(">I", header_bytes[13:17])[0]
    
    return (magic, seq, ptype, dlen, checksum)

def verify_packet(packet_bytes):
    """Verifies packet integrity using checksum and structure validation."""
    if len(packet_bytes) < HEADER_SIZE + IV_SIZE:
        if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Packet too short for header + IV: {len(packet_bytes)}")
        return (False, None, None)
    
    header_bytes = packet_bytes[:HEADER_SIZE]
    iv_plus_encrypted_payload = packet_bytes[HEADER_SIZE:]
    
    try:
        magic, seq, ptype, dlen_from_header, received_checksum = decode_header(header_bytes)
    except Exception as e:
        if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Failed to decode header in verify_packet: {e}")
        return (False, None, None)
    
    if magic != MAGIC_NUMBER:
        if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Invalid magic number: {hex(magic)} != {hex(MAGIC_NUMBER)}")
        return (False, (magic, seq, ptype, dlen_from_header), None)
    
    if len(iv_plus_encrypted_payload) != dlen_from_header:
        if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] (IV + Encrypted Payload) length mismatch: {len(iv_plus_encrypted_payload)} != {dlen_from_header}")
        return (False, (magic, seq, ptype, dlen_from_header), None)
    
    calculated_checksum = calculate_checksum(header_bytes[:13] + iv_plus_encrypted_payload)
    
    is_valid = (calculated_checksum == received_checksum)
    if not is_valid:
        if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Checksum mismatch: {hex(calculated_checksum)} != {hex(received_checksum)}")
    
    decoded_header_info = (magic, seq, ptype, dlen_from_header)
    return (is_valid, decoded_header_info, iv_plus_encrypted_payload if is_valid else None)

def receive_packet(sock, timeout=None):
    """Receives and processes a packet from the socket."""
    original_timeout = sock.gettimeout()
    try:
        if timeout is not None:
            sock.settimeout(timeout)
            
        header_bytes = b''
        while len(header_bytes) < HEADER_SIZE:
            chunk = sock.recv(HEADER_SIZE - len(header_bytes))
            if not chunk:
                if PROTOCOL_VERBOSE_DEBUG: print("[DEBUG] Connection closed while receiving header")
                return (False, None, None)
            header_bytes += chunk
            
        try:
            _, _, _, dlen_iv_plus_encrypted, _ = decode_header(header_bytes)
        except Exception as e:
            if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Failed to decode header to get data length: {e}")
            return (False, None, None)
            
        iv_plus_encrypted_payload_bytes = b''
        if dlen_iv_plus_encrypted < IV_SIZE:
            if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Data length {dlen_iv_plus_encrypted} from header is less than IV_SIZE {IV_SIZE}")
            return (False, None, None)
            
        while len(iv_plus_encrypted_payload_bytes) < dlen_iv_plus_encrypted:
            chunk = sock.recv(min(4096, dlen_iv_plus_encrypted - len(iv_plus_encrypted_payload_bytes)))
            if not chunk:
                if PROTOCOL_VERBOSE_DEBUG: print("[DEBUG] Connection closed while receiving IV+payload")
                return (False, None, None)
            iv_plus_encrypted_payload_bytes += chunk
            
        is_valid, header_info_from_verify, verified_iv_plus_encrypted_payload = verify_packet(header_bytes + iv_plus_encrypted_payload_bytes)
        
        if not is_valid:
            ptype_for_log = header_info_from_verify[2] if header_info_from_verify else "N/A"
            magic_for_log = hex(header_info_from_verify[0]) if header_info_from_verify else "N/A"
            seq_for_log = header_info_from_verify[1] if header_info_from_verify else "N/A"
            if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Packet verification failed: magic={magic_for_log}, seq={seq_for_log}, type={get_packet_type_name(ptype_for_log) if isinstance(ptype_for_log, int) else ptype_for_log}")
            return (False, header_info_from_verify, None)
        
        magic, seq, ptype, _ = header_info_from_verify
        
        iv = verified_iv_plus_encrypted_payload[:IV_SIZE]
        encrypted_actual_payload = verified_iv_plus_encrypted_payload[IV_SIZE:]
        
        try:
            decrypted_payload = _decrypt_payload(encrypted_actual_payload, PRE_SHARED_KEY, iv)
            if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Received and decrypted packet: magic={hex(magic)}, seq={seq}, type={get_packet_type_name(ptype)}, original_len={len(decrypted_payload)}")
            final_decoded_header = (magic, seq, ptype, len(decrypted_payload))
            return (True, final_decoded_header, decrypted_payload)
        except Exception as e:
            if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Payload decryption failed: {e}. magic={hex(magic)}, seq={seq}, type={get_packet_type_name(ptype)}")
            return (False, header_info_from_verify, None)
        
    except socket.timeout:
        if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Socket timeout in receive_packet after {timeout}s")
        return (False, None, None)
    except Exception as e:
        if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Error in receive_packet: {e}")
        return (False, None, None)
    finally:
        if timeout is not None:
            try:
                sock.settimeout(original_timeout)
            except socket.error as se_sock:
                if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Error restoring socket timeout: {se_sock}")

def send_packet(sock, packet_type, payload, max_retries=3):
    """Sends a packet with retry logic for reliability."""
    try:
        packet = create_packet(packet_type, payload)
        payload_len = len(payload.encode() if isinstance(payload, str) else payload)
        if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Sending packet: type={get_packet_type_name(packet_type)}, plaintext_len={payload_len}, packet_len={len(packet)}")
    except Exception as e:
        if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Error creating packet in send_packet: {e}")
        return False
    
    for attempt in range(max_retries):
        try:
            sock.sendall(packet)
            return True
        except (socket.error, BrokenPipeError) as e:
            if PROTOCOL_VERBOSE_DEBUG: print(f"[DEBUG] Send attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                return False
            time.sleep(0.1 * (attempt + 1))
    
    return False

def corrupt_packet(packet_bytes, corruption_rate=0.01):
    """Simulates transmission errors by randomly corrupting packet bytes."""
    corrupted = bytearray(packet_bytes)
    for i in range(len(corrupted)):
        if random.random() < corruption_rate:
            corrupted[i] = random.randint(0, 255)
    return bytes(corrupted)

def handle_corrupted_packet(sock, seq_num):
    """Requests retransmission of a corrupted packet."""
    nack_payload = struct.pack(">I", seq_num)
    send_packet(sock, PACKET_TYPE_ERROR, nack_payload)

def get_packet_type_name(packet_type):
    """Returns a human-readable name for the packet type."""
    packet_types = {
        PACKET_TYPE_USERNAME: "USERNAME",
        PACKET_TYPE_GAME_START: "GAME_START",
        PACKET_TYPE_MOVE: "MOVE",
        PACKET_TYPE_BOARD_UPDATE: "BOARD_UPDATE",
        PACKET_TYPE_GAME_END: "GAME_END",
        PACKET_TYPE_ERROR: "ERROR",
        PACKET_TYPE_DISCONNECT: "DISCONNECT",
        PACKET_TYPE_RECONNECT: "RECONNECT",
        PACKET_TYPE_ACK: "ACK",
        PACKET_TYPE_HEARTBEAT: "HEARTBEAT",
        PACKET_TYPE_CHAT: "CHAT"
    }
    return packet_types.get(packet_type, f"UNKNOWN({packet_type})") 