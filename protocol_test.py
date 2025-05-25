"""
protocol_test.py

Runs a statistical demonstration of the protocol's checksum mechanism.

Default configuration does not provide verbose debug prints - flag can be set to True.
"""

import random
import time
import sys
import protocol
from protocol import (
    create_packet,
    verify_packet,
    corrupt_packet,
    PACKET_TYPE_CHAT,
    PRE_SHARED_KEY
)

def run_checksum_statistical_demo(num_packets_to_test=1000, intentional_corruption_chance=0.5):
    """
    Runs a statistical demonstration of the protocol's checksum mechanism.

    Args:
        num_packets_to_test (int): Total number of packets to generate and test.
        intentional_corruption_chance (float): Probability (0.0 to 1.0) that a generated packet
                                               will be intentionally corrupted.
    """
    print(f"Starting checksum statistical demonstration...")
    print(f"Testing with {num_packets_to_test} packets.")
    print(f"Chance of intentional corruption per packet: {intentional_corruption_chance*100:.1f}%")
    print(f"Using corruption function: protocol.corrupt_packet()")

    stats = {
        'total_tested': 0,
        'total_intentionally_corrupted': 0,
        'total_uncorrupted_sent': 0,
        'correctly_detected_corruption': 0,  # Intentionally corrupted, verify_packet says False
        'missed_corruption': 0,              # Intentionally corrupted, verify_packet says True
        'correctly_passed_valid': 0,         # Not intentionally corrupted, verify_packet says True
        'falsely_detected_corruption': 0     # Not intentionally corrupted, verify_packet says False
    }

    for i in range(num_packets_to_test):
        stats['total_tested'] += 1
        payload_content = f"Test payload data for packet {i} - {time.time_ns()}"
        original_packet = create_packet(PACKET_TYPE_CHAT, payload_content)
        
        packet_to_verify = original_packet
        was_intentionally_corrupted = False

        if random.random() < intentional_corruption_chance:

            temp_packet_list = list(original_packet)
            if len(temp_packet_list) > 0:
                # Flip one bit in the first byte
                # Corrupt a byte in the payload
                corruption_index = random.randint(0, len(temp_packet_list) -1)

                if len(temp_packet_list) > 16 :
                    idx_to_corrupt = random.randint(0, 12)
                    if len(temp_packet_list) > 33 and random.random() < 0.5 :
                         idx_to_corrupt = random.randint(33, len(temp_packet_list) - 1)
                else: # If packet is very short, corrupt the first byte
                    idx_to_corrupt = 0

                original_byte = temp_packet_list[idx_to_corrupt]
                temp_packet_list[idx_to_corrupt] = (original_byte + 1) % 256 
                packet_to_verify = bytes(temp_packet_list)
                was_intentionally_corrupted = True
                stats['total_intentionally_corrupted'] += 1
            else:
                stats['total_uncorrupted_sent'] += 1
        else:
            stats['total_uncorrupted_sent'] += 1

        # Verify the packet
        is_valid_according_to_protocol, _, _ = verify_packet(packet_to_verify)

        if was_intentionally_corrupted:
            if not is_valid_according_to_protocol:
                stats['correctly_detected_corruption'] += 1
            else:
                stats['missed_corruption'] += 1
        else:
            if is_valid_according_to_protocol:
                stats['correctly_passed_valid'] += 1
            else:
                stats['falsely_detected_corruption'] += 1

        if (i + 1) % (num_packets_to_test // 10 if num_packets_to_test >= 10 else 1) == 0:
            print(f"Processed {i+1}/{num_packets_to_test} packets...")

    print("\nChecksum Test Statistics:")
    print(f"Total packets tested: {stats['total_tested']}")
    print(f"Packets sent uncorrupted: {stats['total_uncorrupted_sent']}")
    print(f"Packets intentionally corrupted: {stats['total_intentionally_corrupted']}")
    print("-----------------------------------")
    if stats['total_uncorrupted_sent'] > 0:
        print(f"  For UNCORRUPTED packets:")
        print(f"    Correctly passed as valid: {stats['correctly_passed_valid']} ({stats['correctly_passed_valid']/stats['total_uncorrupted_sent']:.2%})")
        print(f"    Incorrectly flagged as corrupt (false positives): {stats['falsely_detected_corruption']} ({stats['falsely_detected_corruption']/stats['total_uncorrupted_sent']:.2%})")
    if stats['total_intentionally_corrupted'] > 0:
        print(f"  For INTENTIONALLY CORRUPTED packets:")
        print(f"    Correctly detected as corrupt: {stats['correctly_detected_corruption']} ({stats['correctly_detected_corruption']/stats['total_intentionally_corrupted']:.2%})")
        print(f"    Missed (passed as valid): {stats['missed_corruption']} ({stats['missed_corruption']/stats['total_intentionally_corrupted']:.2%})")
    print("-----------------------------------")

if __name__ == "__main__":
    output_filename = "checksum_test_results.txt"
    original_stdout = sys.stdout

    protocol.PROTOCOL_VERBOSE_DEBUG = False

    print(f"Running tests and saving results to: {output_filename}")

    with open(output_filename, 'w') as f:
        sys.stdout = f
        try:
            run_checksum_statistical_demo(num_packets_to_test=10000, intentional_corruption_chance=0.5)
        finally:
            sys.stdout = original_stdout

    print(f"Tests finished. Results saved to {output_filename}")