import unittest
import os
from protocol import (
    create_packet,
    receive_packet,
    verify_packet,
    _encrypt_payload,
    _decrypt_payload,
    PRE_SHARED_KEY,
    IV_SIZE,
    HEADER_SIZE,
    PACKET_TYPE_CHAT,
    MAGIC_NUMBER,
    get_packet_type_name
)

class TestEncryptionProtocol(unittest.TestCase):

    def test_encrypt_decrypt_functions_direct(self):
        """Test direct encryption and decryption functions."""
        key = PRE_SHARED_KEY
        iv = os.urandom(IV_SIZE)
        original_payload = b"Hello, secure world!"

        encrypted = _encrypt_payload(original_payload, key, iv)
        self.assertNotEqual(original_payload, encrypted, "Encrypted data should not be same as original.")

        decrypted = _decrypt_payload(encrypted, key, iv)
        self.assertEqual(original_payload, decrypted, "Decrypted data does not match original.")

    def test_packet_creation_and_reception_roundtrip(self):
        """Test that a created packet can be received and its payload decrypted correctly."""
        original_payload_str = "This is a test message for packet roundtrip."
        packet_type = PACKET_TYPE_CHAT

        # Simulate sending a packet
        sent_packet_bytes = create_packet(packet_type, original_payload_str)
        self.assertIsNotNone(sent_packet_bytes)

        # Simulate receiving the packet
        # We need a mock socket or to dissect the packet for receive_packet
        # For simplicity, we'll manually reconstruct what receive_packet needs
        
        # 1. Verify and extract IV + encrypted payload
        is_valid_verify, header_info_verify, iv_plus_encrypted = verify_packet(sent_packet_bytes)
        self.assertTrue(is_valid_verify, f"Packet verification failed. Header: {header_info_verify}")
        self.assertIsNotNone(iv_plus_encrypted)
        
        magic, seq, ptype, dlen_iv_plus_encrypted = header_info_verify
        self.assertEqual(magic, MAGIC_NUMBER)
        self.assertEqual(ptype, packet_type)
        self.assertEqual(len(iv_plus_encrypted), dlen_iv_plus_encrypted)

        # 2. Extract IV and decrypt
        iv = iv_plus_encrypted[:IV_SIZE]
        encrypted_actual_payload = iv_plus_encrypted[IV_SIZE:]
        
        decrypted_payload_bytes = _decrypt_payload(encrypted_actual_payload, PRE_SHARED_KEY, iv)
        self.assertEqual(original_payload_str.encode(), decrypted_payload_bytes)

    def test_iv_uniqueness_leads_to_different_ciphertexts(self):
        """Test that encrypting the same payload twice yields different ciphertexts (due to IV)."""
        payload_str = "Same payload, different IVs."
        packet_type = PACKET_TYPE_CHAT

        packet1_bytes = create_packet(packet_type, payload_str)
        packet2_bytes = create_packet(packet_type, payload_str)

        # Extract ciphertext part (after header and IV)
        ciphertext1 = packet1_bytes[HEADER_SIZE + IV_SIZE:]
        ciphertext2 = packet2_bytes[HEADER_SIZE + IV_SIZE:]
        
        # Extract IVs
        iv1 = packet1_bytes[HEADER_SIZE : HEADER_SIZE + IV_SIZE]
        iv2 = packet2_bytes[HEADER_SIZE : HEADER_SIZE + IV_SIZE]

        self.assertNotEqual(iv1, iv2, "IVs should be different for two packets.")
        self.assertNotEqual(ciphertext1, ciphertext2, "Ciphertexts should be different due to different IVs.")

    def test_checksum_valid_packet(self):
        """Test that a normally created packet passes checksum verification."""
        payload_str = "Valid packet test."
        packet_type = PACKET_TYPE_CHAT
        packet_bytes = create_packet(packet_type, payload_str)
        
        is_valid, _, _ = verify_packet(packet_bytes)
        self.assertTrue(is_valid, "Validly created packet failed checksum verification.")

    def test_checksum_corrupted_iv(self):
        """Test that tampering with the IV makes the packet fail checksum."""
        payload_str = "Tamper IV test."
        packet_type = PACKET_TYPE_CHAT
        packet_bytes_list = list(create_packet(packet_type, payload_str))

        # Corrupt one byte of the IV (e.g., the first byte of IV)
        # IV starts after HEADER_SIZE
        iv_start_index = HEADER_SIZE
        if len(packet_bytes_list) > iv_start_index:
            packet_bytes_list[iv_start_index] = (packet_bytes_list[iv_start_index] + 1) % 256
        else:
            self.fail("Packet too short to corrupt IV.")

        corrupted_packet_bytes = bytes(packet_bytes_list)
        is_valid, header_info, _ = verify_packet(corrupted_packet_bytes)
        self.assertFalse(is_valid, "Packet with corrupted IV unexpectedly passed checksum verification.")
        if header_info: # If header could be parsed
            print(f"Corrupted IV test debug - Header: {header_info}, Type: {get_packet_type_name(header_info[2])}")


    def test_checksum_corrupted_ciphertext(self):
        """Test that tampering with the ciphertext makes the packet fail checksum."""
        payload_str = "Tamper ciphertext test."
        packet_type = PACKET_TYPE_CHAT
        packet_bytes_list = list(create_packet(packet_type, payload_str))

        # Corrupt one byte of the ciphertext (e.g., the first byte after IV)
        ciphertext_start_index = HEADER_SIZE + IV_SIZE
        if len(packet_bytes_list) > ciphertext_start_index:
            packet_bytes_list[ciphertext_start_index] = (packet_bytes_list[ciphertext_start_index] + 1) % 256
        else:
            self.fail("Packet too short to corrupt ciphertext.")
            
        corrupted_packet_bytes = bytes(packet_bytes_list)
        is_valid, header_info, _ = verify_packet(corrupted_packet_bytes)
        self.assertFalse(is_valid, "Packet with corrupted ciphertext unexpectedly passed checksum verification.")
        if header_info:
             print(f"Corrupted Ciphertext test debug - Header: {header_info}, Type: {get_packet_type_name(header_info[2])}")

    def test_empty_payload_encryption(self):
        """Test encryption and decryption of an empty payload."""
        original_payload_str = ""
        packet_type = PACKET_TYPE_CHAT

        sent_packet_bytes = create_packet(packet_type, original_payload_str)
        self.assertIsNotNone(sent_packet_bytes)
        
        is_valid_verify, header_info_verify, iv_plus_encrypted = verify_packet(sent_packet_bytes)
        self.assertTrue(is_valid_verify)
        
        iv = iv_plus_encrypted[:IV_SIZE]
        encrypted_actual_payload = iv_plus_encrypted[IV_SIZE:]
        
        decrypted_payload_bytes = _decrypt_payload(encrypted_actual_payload, PRE_SHARED_KEY, iv)
        self.assertEqual(original_payload_str.encode(), decrypted_payload_bytes, "Decrypted empty payload does not match original.")


if __name__ == '__main__':
    unittest.main()

        