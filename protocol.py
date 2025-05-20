"""
protocol.py

Implements a custom packet protocol for Battleship game communication.
Includes packet structure, serialization, checksum verification, and AES encryption.
"""

import struct
import binascii
import random
import time
import socket
import os # For IV generation
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

# Magic number to identify our protocol (hex value for "BSHP")
MAGIC_NUMBER = 0x42534850

# Packet types
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

# Packet header format:
# - Magic Number (4 bytes) - Identifies protocol
# - Sequence Number (4 bytes) - Used for ordering and detecting missing packets
# - Packet Type (1 byte) - Identifies the type of packet
# - Data Length (4 bytes) - Length of the (IV + encrypted payload) in bytes
# - Checksum (4 bytes) - CRC32 checksum for data integrity
# Total header size: 17 bytes
# Followed by IV (16 bytes) + variable-length encrypted payload data

HEADER_SIZE = 17
HEADER_FORMAT = ">IIBI" # unsigned int, unsigned int, unsigned char, unsigned int

# Encryption constants
AES_KEY_SIZE = 32  # 256-bit key
IV_SIZE = 16       # AES block size, 128-bit IV for CTR mode

# PRE-SHARED KEY (PSK) - IMPORTANT: This should be securely managed and distributed out-of-band.
# For demonstration, it's hardcoded. Replace with a securely generated key.
PRE_SHARED_KEY = b'\x00' * AES_KEY_SIZE # Replace with your actual 32-byte key
# Example: PRE_SHARED_KEY = os.urandom(AES_KEY_SIZE)
# print(f"Using PSK: {PRE_SHARED_KEY.hex()}") # For debugging if you generate one

# Global sequence number counter
next_sequence_number = 0

def get_next_sequence_number():
    """Get the next sequence number for packet sending"""
    global next_sequence_number
    seq = next_sequence_number
    next_sequence_number = (next_sequence_number + 1) % 0xFFFFFFFF  # Wrap around at max 32-bit value
    return seq

def calculate_checksum(data):
    """Calculate CRC32 checksum for the given data"""
    return binascii.crc32(data) & 0xFFFFFFFF  # Ensure 32-bit unsigned value

def _encrypt_payload(payload_bytes, key, iv):
    """Encrypts payload using AES-CTR."""
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(payload_bytes) + encryptor.finalize()

def _decrypt_payload(encrypted_payload_bytes, key, iv):
    """Decrypts payload using AES-CTR."""
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    return decryptor.update(encrypted_payload_bytes) + decryptor.finalize()

def create_packet(packet_type, payload):
    """
    Create a packet with the specified type and payload.
    The payload is encrypted before packet creation.
    Returns the complete packet as bytes.
    """
    seq_num = get_next_sequence_number()
    payload_bytes = payload.encode() if isinstance(payload, str) else payload
    
    # Generate IV and encrypt payload
    iv = os.urandom(IV_SIZE)
    encrypted_payload = _encrypt_payload(payload_bytes, PRE_SHARED_KEY, iv)
    
    # Data length is IV size + encrypted payload size
    data_length = IV_SIZE + len(encrypted_payload)
    
    # Create header without checksum
    header_prefix = struct.pack(HEADER_FORMAT, 
                             MAGIC_NUMBER, 
                             seq_num, 
                             packet_type, 
                             data_length)
    
    # Data to checksum: header_prefix + IV + encrypted_payload
    data_to_checksum = header_prefix + iv + encrypted_payload
    checksum = calculate_checksum(data_to_checksum)
    
    # Add checksum to header prefix to get full header
    full_header = header_prefix + struct.pack(">I", checksum)
    
    # Combine full_header, IV, and encrypted_payload
    return full_header + iv + encrypted_payload

def decode_header(header_bytes):
    """
    Decode the packet header.
    Returns a tuple of (magic_number, sequence_number, packet_type, data_length, checksum).
    data_length is the length of (IV + encrypted_payload).
    """
    if len(header_bytes) != HEADER_SIZE:
        raise ValueError(f"Invalid header size: {len(header_bytes)}, expected {HEADER_SIZE}")
    
    magic, seq, ptype, dlen = struct.unpack(HEADER_FORMAT, header_bytes[:13])
    checksum = struct.unpack(">I", header_bytes[13:17])[0]
    
    return (magic, seq, ptype, dlen, checksum)

def verify_packet(packet_bytes):
    """
    Verify packet integrity using the checksum.
    The payload of packet_bytes is (IV + encrypted_payload).
    Returns a tuple of (is_valid, decoded_header_info, iv_plus_encrypted_payload).
    decoded_header_info = (magic, seq, ptype, dlen_of_iv_plus_encrypted_payload)
    If is_valid is False, iv_plus_encrypted_payload will be None.
    """
    if len(packet_bytes) < HEADER_SIZE + IV_SIZE: # Must have header and IV
        print(f"[DEBUG] Packet too short for header + IV: {len(packet_bytes)}")
        return (False, None, None)
    
    header_bytes = packet_bytes[:HEADER_SIZE]
    iv_plus_encrypted_payload = packet_bytes[HEADER_SIZE:]
    
    try:
        magic, seq, ptype, dlen_from_header, received_checksum = decode_header(header_bytes)
    except Exception as e:
        print(f"[DEBUG] Failed to decode header in verify_packet: {e}")
        return (False, None, None)
    
    # Verify magic number
    if magic != MAGIC_NUMBER:
        print(f"[DEBUG] Invalid magic number: {hex(magic)} != {hex(MAGIC_NUMBER)}")
        return (False, (magic, seq, ptype, dlen_from_header), None)
    
    # Verify payload length (dlen_from_header is length of IV + encrypted_payload)
    if len(iv_plus_encrypted_payload) != dlen_from_header:
        print(f"[DEBUG] (IV + Encrypted Payload) length mismatch: {len(iv_plus_encrypted_payload)} != {dlen_from_header}")
        return (False, (magic, seq, ptype, dlen_from_header), None)
    
    # Calculate checksum of header_prefix + (IV + encrypted_payload)
    # header_prefix is the first 13 bytes of header_bytes
    calculated_checksum = calculate_checksum(header_bytes[:13] + iv_plus_encrypted_payload)
    
    # Verify checksum
    is_valid = (calculated_checksum == received_checksum)
    if not is_valid:
        print(f"[DEBUG] Checksum mismatch: {hex(calculated_checksum)} != {hex(received_checksum)}")
    
    decoded_header_info = (magic, seq, ptype, dlen_from_header)
    return (is_valid, decoded_header_info, iv_plus_encrypted_payload if is_valid else None)

def receive_packet(sock, timeout=None):
    """
    Receive a packet from the socket, verify, and decrypt its payload.
    Returns a tuple of (is_valid, final_decoded_header, decrypted_payload).
    final_decoded_header = (magic, seq, ptype, dlen_of_decrypted_payload)
    """
    original_timeout = sock.gettimeout()
    try:
        if timeout is not None:
            sock.settimeout(timeout)
            
        # Receive header
        header_bytes = b''
        while len(header_bytes) < HEADER_SIZE:
            chunk = sock.recv(HEADER_SIZE - len(header_bytes))
            if not chunk:
                print("[DEBUG] Connection closed while receiving header")
                return (False, None, None)  # Connection closed
            header_bytes += chunk
            
        # Decode header to get length of (IV + encrypted_payload)
        try:
            _, _, _, dlen_iv_plus_encrypted, _ = decode_header(header_bytes)
        except Exception as e:
            print(f"[DEBUG] Failed to decode header to get data length: {e}")
            return (False, None, None)
            
        # Receive IV + encrypted_payload
        iv_plus_encrypted_payload_bytes = b''
        if dlen_iv_plus_encrypted < IV_SIZE:
            print(f"[DEBUG] Data length {dlen_iv_plus_encrypted} from header is less than IV_SIZE {IV_SIZE}")
            return (False, None, None) # Invalid length
            
        while len(iv_plus_encrypted_payload_bytes) < dlen_iv_plus_encrypted:
            chunk = sock.recv(min(4096, dlen_iv_plus_encrypted - len(iv_plus_encrypted_payload_bytes)))
            if not chunk:
                print("[DEBUG] Connection closed while receiving IV+payload")
                return (False, None, None)  # Connection closed
            iv_plus_encrypted_payload_bytes += chunk
            
        # Verify complete packet (header + IV + encrypted_payload)
        is_valid, header_info_from_verify, verified_iv_plus_encrypted_payload = verify_packet(header_bytes + iv_plus_encrypted_payload_bytes)
        
        if not is_valid:
            # Get packet type from header_info_for_verify for logging if available
            ptype_for_log = header_info_from_verify[2] if header_info_from_verify else "N/A"
            magic_for_log = hex(header_info_from_verify[0]) if header_info_from_verify else "N/A"
            seq_for_log = header_info_from_verify[1] if header_info_from_verify else "N/A"
            print(f"[DEBUG] Packet verification failed: magic={magic_for_log}, seq={seq_for_log}, type={get_packet_type_name(ptype_for_log) if isinstance(ptype_for_log, int) else ptype_for_log}")
            return (False, header_info_from_verify, None)
        
        # If valid, extract IV, decrypt payload
        magic, seq, ptype, _ = header_info_from_verify # dlen here is for IV+encrypted
        
        iv = verified_iv_plus_encrypted_payload[:IV_SIZE]
        encrypted_actual_payload = verified_iv_plus_encrypted_payload[IV_SIZE:]
        
        try:
            decrypted_payload = _decrypt_payload(encrypted_actual_payload, PRE_SHARED_KEY, iv)
            print(f"[DEBUG] Received and decrypted packet: magic={hex(magic)}, seq={seq}, type={get_packet_type_name(ptype)}, original_len={len(decrypted_payload)}")
            final_decoded_header = (magic, seq, ptype, len(decrypted_payload))
            return (True, final_decoded_header, decrypted_payload)
        except Exception as e:
            print(f"[DEBUG] Payload decryption failed: {e}. magic={hex(magic)}, seq={seq}, type={get_packet_type_name(ptype)}")
            return (False, header_info_from_verify, None) # Return raw header info from verify
        
    except socket.timeout:
        # This is an expected condition if timeout is set
        print(f"[DEBUG] Socket timeout in receive_packet after {timeout}s")
        return (False, None, None)
    except Exception as e:
        print(f"[DEBUG] Error in receive_packet: {e}")
        return (False, None, None)
    finally:
        # Restore original timeout
        if timeout is not None:
            try:
                sock.settimeout(original_timeout)
            except socket.error as se_sock:
                print(f"[DEBUG] Error restoring socket timeout: {se_sock}")

def send_packet(sock, packet_type, payload, max_retries=3):
    """
    Send a packet with retry logic if needed.
    Payload is encrypted by create_packet.
    Returns True if the packet was sent successfully, False otherwise.
    """
    try:
        packet = create_packet(packet_type, payload) # payload is plaintext here
        payload_len = len(payload.encode() if isinstance(payload, str) else payload)
        print(f"[DEBUG] Sending packet: type={get_packet_type_name(packet_type)}, plaintext_len={payload_len}, packet_len={len(packet)}")
    except Exception as e:
        print(f"[DEBUG] Error creating packet in send_packet: {e}")
        return False
    
    for attempt in range(max_retries):
        try:
            sock.sendall(packet)
            return True
        except (socket.error, BrokenPipeError) as e:
            print(f"[DEBUG] Send attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                return False
            time.sleep(0.1 * (attempt + 1))  # Backoff before retry
    
    return False

# For testing/debugging purposes
def corrupt_packet(packet_bytes, corruption_rate=0.01):
    """
    Randomly corrupt some bytes in the packet to simulate transmission errors.
    corruption_rate is the probability of corrupting each byte.
    """
    corrupted = bytearray(packet_bytes)
    for i in range(len(corrupted)):
        if random.random() < corruption_rate:
            corrupted[i] = random.randint(0, 255)
    return bytes(corrupted)

# Error handling policies
def handle_corrupted_packet(sock, seq_num):
    """
    Request retransmission of a corrupted packet.
    This is a simple implementation - would need to be expanded for a full 
    reliable protocol implementation.
    """
    # Send negative ACK (retransmission request)
    nack_payload = struct.pack(">I", seq_num)
    send_packet(sock, PACKET_TYPE_ERROR, nack_payload)

def get_packet_type_name(packet_type):
    """Get a string representation of the packet type"""
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