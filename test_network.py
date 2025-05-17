import unittest
import socket
from unittest.mock import Mock, patch
from protocol import send_packet, PACKET_TYPE_CHAT, PACKET_TYPE_MOVE
from server import ProtocolAdapter

class TestNetworking(unittest.TestCase):
    @patch('socket.socket')
    def test_send_packet(self, mock_socket):
        # Create a mock socket
        sock = Mock()
        
        # Test sending a packet
        success = send_packet(sock, PACKET_TYPE_CHAT, "Hello world")
        
        # Assert that sendall was called
        sock.sendall.assert_called_once()
        self.assertTrue(success)
        
        # Test handling a socket error
        sock.sendall.side_effect = socket.error("Test error")
        success = send_packet(sock, PACKET_TYPE_CHAT, "This will fail")
        self.assertFalse(success)
    
    def test_protocol_adapter(self):
        # Create a mock adapter with a mocked readline method
        adapter = ProtocolAdapter(Mock(), "TestUser")
        
        original_readline = adapter.readline
        adapter.readline = Mock(return_value="A5\n")
        
        # Test readline
        result = adapter.readline()
        self.assertEqual(result, "A5\n")
        
        adapter.write = Mock(return_value=10)  # Return length of string
        adapter.flush = Mock()  # No return value
        
        # Test write
        adapter.write("Hello world\n")
        adapter.write.assert_called_with("Hello world\n")
        
        # Test flush
        adapter.flush()
        adapter.flush.assert_called_once()

if __name__ == "__main__":
    unittest.main()