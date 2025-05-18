"""
client.py

Connects to a Battleship server for a two-player game.
This client handles both single-player and two-player modes:
- Receives and displays game boards and messages from the server
- Sends user commands for ship placement and firing coordinates
- Runs in a threaded mode to handle asynchronous server messages
- Supports playing multiple games in succession without disconnecting
- Provides feedback about move timeouts
- Uses custom protocol
- Supports reconnection to an interrupted game
"""

import socket
import threading
import time
import os
import json
from protocol import (
    receive_packet, send_packet, 
    PACKET_TYPE_USERNAME, PACKET_TYPE_MOVE, PACKET_TYPE_CHAT,
    PACKET_TYPE_DISCONNECT, PACKET_TYPE_RECONNECT, PACKET_TYPE_HEARTBEAT,
    PACKET_TYPE_GAME_START, PACKET_TYPE_BOARD_UPDATE, PACKET_TYPE_GAME_END,
    PACKET_TYPE_ERROR, PACKET_TYPE_ACK, get_packet_type_name
)

HOST = '127.0.0.1'
PORT = 5001

# Flag (global) indicating if the client should stop running
running = True

# Store username for reconnection purposes
current_username = ""

# Get client ID from environment variable or create a unique one
client_id = os.environ.get('BATTLESHIP_CLIENT_ID', str(int(time.time())))
previous_connection_file = f".battleship_connection_{client_id}.json"

def save_connection_info(username):
    """Save username to a file for reconnection"""
    try:
        with open(previous_connection_file, 'w') as f:
            json.dump({'username': username, 'timestamp': time.time()}, f)
    except:
        print("[WARNING] Could not save connection information.")

def load_connection_info():
    """Load previous username if available and not expired"""
    try:
        if os.path.exists(previous_connection_file):
            with open(previous_connection_file, 'r') as f:
                data = json.load(f)
                
                # Check if reconnection window is still valid (60 seconds)
                elapsed = time.time() - data.get('timestamp', 0)
                if elapsed <= 60:
                    return data.get('username'), True
                else:
                    # Connection is too old for reconnection
                    return data.get('username'), False
    except:
        pass
    
    return None, False

def receive_messages(sock):
    """
    Continuously receive and display messages from the server using the protocol
    """
    global running

    while running:
        try:
            # Use protocol's receive_packet instead of readline
            valid, header, payload = receive_packet(sock)
            
            if not valid or not payload:
                print("\n[ERROR] Server disconnected or sent corrupted data. Please restart the client to reconnect.")
                running = False
                break
            
            magic, seq, packet_type, data_len = header
            payload_str = payload.decode() if isinstance(payload, bytes) else payload
            
            # Process different packet types
            if packet_type == PACKET_TYPE_BOARD_UPDATE:
                # Board updates will be sent as a formatted string payload
                print("\n" + payload_str)
            elif packet_type == PACKET_TYPE_GAME_START:
                print(f"\n[GAME START] {payload_str}")
            elif packet_type == PACKET_TYPE_GAME_END:
                print(f"\n[GAME END] {payload_str}")
            elif packet_type == PACKET_TYPE_ERROR:
                print(f"\n[ERROR] {payload_str}")
                if "timeout" in payload_str.lower() or "timed out" in payload_str.lower():
                    print("[ATTENTION] You have timed out! Please respond to avoid forfeiting your turn in future.")
            elif packet_type == PACKET_TYPE_RECONNECT:
                print(f"\n[RECONNECTED] {payload_str}")
            elif packet_type == PACKET_TYPE_HEARTBEAT:
                # Respond to heartbeat with ACK to maintain connection
                send_packet(sock, PACKET_TYPE_ACK, str(seq))
            else:
                # Regular message (chat, etc.)
                print(payload_str)
                
        except ConnectionResetError:
            print("\n[ERROR] Connection to server was reset. Please restart the client to reconnect.")
            print(f"[INFO] Your username '{current_username}' was saved for reconnection.")
            save_connection_info(current_username)
            running = False
            break
        except BrokenPipeError:
            print("\n[ERROR] Connection to server was broken. Please restart the client to reconnect.")
            print(f"[INFO] Your username '{current_username}' was saved for reconnection.")
            save_connection_info(current_username)
            running = False
            break
        except Exception as e:
            print(f"\n[ERROR] Error receiving from server: {e}")
            running = False
            break

def main():
    global running, current_username

    print("[INFO] Connecting to Battleship server...")
    try:
        # Check for previous connection data for reconnection
        previous_username, is_recent = load_connection_info()
        
        # Only prompt for reconnection if the previous connection was recent
        if previous_username and is_recent:
            print(f"[INFO] Recent disconnection found for username: {previous_username}")
            reconnect = input(f"Would you like to reconnect as {previous_username}? (y/n): ").strip().lower()
            if reconnect == 'y':
                username = previous_username
                print(f"[INFO] Reconnecting as {username}...")
            else:
                # Delete the connection file if not reconnecting
                try:
                    os.remove(previous_connection_file)
                except:
                    pass
                    
                username = ""
                while not username:
                    username = input("Enter your username: ").strip()
                    if not username:
                        print("[ERROR] Username cannot be empty. Please try again.")
        else:
            # Get username from user
            username = ""
            while not username:
                username = input("Enter your username: ").strip()
                if not username:
                    print("[ERROR] Username cannot be empty. Please try again.")
        
        # Store username globally for reconnection
        current_username = username

        # Connect to server
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((HOST, PORT))
            print(f"[INFO] Connected to server at {HOST}:{PORT}")
            
            # Send username using protocol
            if send_packet(s, PACKET_TYPE_USERNAME, username):
                print(f"[INFO] Username '{username}' sent to server.")
            else:
                print("[ERROR] Failed to send username to server")
                running = False
                return # Exit if username cannot be sent

            # Save connection info for potential reconnection
            save_connection_info(username)

            # Start a thread for receiving messages
            receive_thread = threading.Thread(target=receive_messages, args=(s,))
            receive_thread.daemon = True # This ensures the thread will exit when the main thread exits
            receive_thread.start()

            try:
                # Main thread handles user input
                while running:
                    try:
                        user_input = input(">> ")

                        if user_input.lower() == 'quit':
                            print("[INFO] Quitting the game...")
                            send_packet(s, PACKET_TYPE_DISCONNECT, "Quit requested by user")
                            # Remove connection file when quitting deliberately
                            try:
                                os.remove(previous_connection_file)
                            except:
                                pass
                            running = False
                            break
                        
                        # Send user input to server using the protocol
                        # Determine packet type based on input
                        if user_input.lower().startswith("fire ") or user_input.upper() in ["H", "V"] or any(c.isalpha() and c.upper() in "ABCDEFGHIJ" for c in user_input):
                            # This is likely a game move (fire command or ship placement)
                            if not send_packet(s, PACKET_TYPE_MOVE, user_input):
                                print("[ERROR] Failed to send move to server")
                        else:
                            # Treat as chat/general command
                            if not send_packet(s, PACKET_TYPE_CHAT, user_input):
                                print("[ERROR] Failed to send message to server")

                    except KeyboardInterrupt:
                        print("\n[INFO] Client exiting due to keyboard interrupt.")
                        send_packet(s, PACKET_TYPE_DISCONNECT, "Keyboard interrupt")
                        # Remove connection file on clean exit
                        try:
                            os.remove(previous_connection_file)
                        except:
                            pass
                        running = False
                        break
                    except EOFError:
                        print("\n[INFO] End of input reached. Exiting...")
                        send_packet(s, PACKET_TYPE_DISCONNECT, "EOF reached")
                        running = False
                        break
                    except Exception as e:
                        print(f"\n[ERROR] Unexpected error: {e}")
                        running = False
                        break

            except KeyboardInterrupt:
                print("\n[INFO] Client exiting due to keyboard interrupt.")
                send_packet(s, PACKET_TYPE_DISCONNECT, "Keyboard interrupt")
                running = False
            
            # Give the receive thread time to display any final messages
            time.sleep(0.5)
            
    except ConnectionRefusedError:
        print(f"[ERROR] Could not connect to server at {HOST}:{PORT} - Check that the server is running.")
    except Exception as e:
        print(f"[ERROR] Connection error: {e}")

if __name__ == "__main__":
    main()
    print("[INFO] Client terminated.")