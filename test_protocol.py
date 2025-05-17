import unittest
from protocol import (
    create_packet, decode_header, verify_packet, calculate_checksum,
    PACKET_TYPE_CHAT, PACKET_TYPE_MOVE, MAGIC_NUMBER, HEADER_SIZE
)

class TestProtocol(unittest.TestCase):
    def test_packet_creation_and_verification(self):
        # Test creating and verifying a valid packet
        test_payload = "test message"
        packet = create_packet(PACKET_TYPE_CHAT, test_payload)
        is_valid, header, payload = verify_packet(packet)

        self.assertTrue(is_valid)
        self.assertEqual(payload.decode(), test_payload)
        magic, seq, packet_type, data_len = header
        self.assertEqual(magic, MAGIC_NUMBER)
        self.assertEqual(packet_type, PACKET_TYPE_CHAT)

    def test_packet_corruption(self):
        packet = create_packet(PACKET_TYPE_MOVE, "A5")
        # Test creating and verifying a valid packet
        corrupted_packet = bytearray(packet)
        corrupted_packet[5] = (corrupted_packet[5] + 1) % 256

        is_valid, _, _ = verify_packet(bytes(corrupted_packet))
        self.assertFalse(is_valid)

    def test_checksum_calculation(self):
        payload = "test message"
        payload_bytes = payload.encode()

        packet = create_packet(PACKET_TYPE_CHAT, payload)

        
        # Create header without checksum (first 13 bytes)
        header_without_checksum = packet[:13]
        
        # Calculate checksum the same way it's done in create_packet
        correct_checksum = calculate_checksum(header_without_checksum + payload_bytes)
        
        # Get the checksum from the packet header
        _, _, _, _, packet_checksum = decode_header(packet[:HEADER_SIZE])
        
        self.assertEqual(packet_checksum, correct_checksum)
        

if __name__ == '__main__':
    unittest.main()

        