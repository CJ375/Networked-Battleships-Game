"""
protocol.py

Implements a custom packet protocol for Battleship game communication.
Includes packet structure, serialization, and checksum verification.
"""

import struct
import binascii
import random
import time
import socket

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
# - Magic Number (4 bytes) - Identifies our protocol
# - Sequence Number (4 bytes) - Used for ordering and detecting missing packets
# - Packet Type (1 byte) - Identifies the type of packet
# - Data Length (4 bytes) - Length of the payload in bytes
# - Checksum (4 bytes) - CRC32 checksum for data integrity
# Total header size: 17 bytes
# Then followed by variable-length payload data

HEADER_SIZE = 17
HEADER_FORMAT = ">IIBI" # unsigned int, unsigned int, unsigned char, unsigned int

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

def create_packet(packet_type, payload):
    """
    Create a packet with the specified type and payload.
    Returns the complete packet as bytes.
    """
    seq_num = get_next_sequence_number()
    payload_bytes = payload.encode() if isinstance(payload, str) else payload
    data_length = len(payload_bytes)
    
    # Create header without checksum
    header = struct.pack(HEADER_FORMAT, 
                         MAGIC_NUMBER, 
                         seq_num, 
                         packet_type, 
                         data_length)
    
    # Calculate checksum of header + payload
    checksum = calculate_checksum(header + payload_bytes)
    
    # Add checksum to header
    full_header = header + struct.pack(">I", checksum)
    
    # Combine header and payload
    return full_header + payload_bytes

def decode_header(header_bytes):
    """
    Decode the packet header.
    Returns a tuple of (magic_number, sequence_number, packet_type, data_length, checksum).
    """
    if len(header_bytes) != HEADER_SIZE:
        raise ValueError(f"Invalid header size: {len(header_bytes)}, expected {HEADER_SIZE}")
    
    magic, seq, ptype, dlen = struct.unpack(HEADER_FORMAT, header_bytes[:13])
    checksum = struct.unpack(">I", header_bytes[13:17])[0]
    
    return (magic, seq, ptype, dlen, checksum)

def verify_packet(packet_bytes):
    """
    Verify packet integrity using the checksum.
    Returns a tuple of (is_valid, decoded_header, payload).
    If is_valid is False, payload will be None.
    """
    if len(packet_bytes) < HEADER_SIZE:
        return (False, None, None)
    
    header_bytes = packet_bytes[:HEADER_SIZE]
    try:
        magic, seq, ptype, dlen, received_checksum = decode_header(header_bytes)
    except:
        return (False, None, None)
    
    # Verify magic number
    if magic != MAGIC_NUMBER:
        return (False, None, None)
    
    payload = packet_bytes[HEADER_SIZE:]
    
    # Verify payload length
    if len(payload) != dlen:
        return (False, None, None)
    
    # Calculate checksum of header (excluding checksum field) + payload
    calculated_checksum = calculate_checksum(packet_bytes[:13] + payload)
    
    # Verify checksum
    is_valid = (calculated_checksum == received_checksum)
    
    decoded_header = (magic, seq, ptype, dlen)
    return (is_valid, decoded_header, payload if is_valid else None)

def receive_packet(sock, timeout=None):
    """
    Receive a packet from the socket.
    Returns a tuple of (is_valid, decoded_header, payload).
    
    If timeout is specified, sets a socket timeout for this operation.
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
                return (False, None, None)  # Connection closed
            header_bytes += chunk
            
        # Decode header to get payload length
        try:
            magic, seq, ptype, dlen, checksum = decode_header(header_bytes)
        except:
            return (False, None, None)
            
        # Receive payload
        payload = b''
        while len(payload) < dlen:
            chunk = sock.recv(min(4096, dlen - len(payload)))
            if not chunk:
                return (False, None, None)  # Connection closed
            payload += chunk
            
        # Verify complete packet
        return verify_packet(header_bytes + payload)
        
    finally:
        # Restore original timeout
        if timeout is not None:
            sock.settimeout(original_timeout)

def send_packet(sock, packet_type, payload, max_retries=3):
    """
    Send a packet with retry logic if needed.
    Returns True if the packet was sent successfully, False otherwise.
    """
    packet = create_packet(packet_type, payload)
    
    for attempt in range(max_retries):
        try:
            sock.sendall(packet)
            return True
        except (socket.error, BrokenPipeError) as e:
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