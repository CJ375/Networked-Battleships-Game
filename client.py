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

# Flag indicating if the user is in spectator mode
is_spectator = False

# Define the storage directory for connection files within the project folder
# This uses the current working directory, assuming the script is run from the project root.
project_root = os.getcwd() 
battleship_dir = os.path.join(project_root, ".reconnection_data")
os.makedirs(battleship_dir, exist_ok=True) # Ensure the directory exists

# Create a unique connection file per username to allow multiple players on the same machine
def get_connection_file(username):
    """Get the connection file path for a specific username"""
    if not username:
        return None
    # Store connection info in a unique file per username within this directory
    # This allows multiple players on the same machine to reconnect without conflicts
    return os.path.join(battleship_dir, f".battleship_connection_{username}.json")

def save_connection_info(username):
    """Save username to a file for reconnection"""
    if not username:
        return
        
    connection_file = get_connection_file(username)
    try:
        with open(connection_file, 'w') as f:
            json.dump({
                'username': username, 
                'timestamp': time.time(),
                'disconnected': True 
            }, f)
            print(f"[DEBUG] Saved connection info for '{username}' to {connection_file}")
    except Exception as e:
        print(f"[WARNING] Could not save connection information: {e}")

def mark_connection_active(username):
    """Mark a connection as active (not disconnected)"""
    if not username:
        return
        
    connection_file = get_connection_file(username)
    try:
        # Only update if file exists
        if os.path.exists(connection_file):
            with open(connection_file, 'r+') as f:
                try:
                    data = json.load(f)
                    data['disconnected'] = False
                    data['timestamp'] = time.time()
                    
                    # Reset file pointer and write updated data
                    f.seek(0)
                    json.dump(data, f)
                    f.truncate()
                except:
                    pass
    except Exception as e:
        print(f"[DEBUG] Error marking connection active: {e}")

def load_connection_info(username):
    """Load previous username connection if available and not expired"""
    if not username:
        return False
        
    connection_file = get_connection_file(username)
    try:
        if os.path.exists(connection_file):
            with open(connection_file, 'r') as f:
                data = json.load(f)
                
                # Check if reconnection window is still valid (60 seconds)
                elapsed = time.time() - data.get('timestamp', 0)
                if elapsed <= 60:
                    print(f"[DEBUG] Found recent connection file for '{username}' ({elapsed:.1f}s old)")
                    return True
                else:
                    # Connection is too old for reconnection
                    print(f"[DEBUG] Connection file found but expired ({elapsed:.1f}s > 60s)")
                    try:
                        os.remove(connection_file)
                    except:
                        pass
    except Exception as e:
        print(f"[DEBUG] Error loading connection info: {e}")
    
    return False

def check_any_recent_connections():
    """Check if there are any recent connection files and return a list of usernames"""
    recent_usernames = []
    
    try:
        # Check all files in the battleship directory
        for filename in os.listdir(battleship_dir):
            if filename.startswith(".battleship_connection_") and filename.endswith(".json"):
                connection_file = os.path.join(battleship_dir, filename)
                try:
                    with open(connection_file, 'r') as f:
                        data = json.load(f)
                        username = data.get('username')
                        timestamp = data.get('timestamp', 0)
                        disconnected = data.get('disconnected', False)  # Default to False if not set
                        
                        # Only include if it's a disconnected connection
                        # Check if connection is recent (within 60 seconds)
                        elapsed = time.time() - timestamp
                        if elapsed <= 60 and username and disconnected:
                            recent_usernames.append((username, elapsed))
                except:
                    continue
    except Exception as e:
        print(f"[DEBUG] Error checking recent connections: {e}")
    
    # Sort by most recent first
    recent_usernames.sort(key=lambda x: x[1])
    return recent_usernames

def receive_messages(sock):
    """
    Continuously receive and display messages from the server using the protocol
    """
    global running, is_spectator
    
    # Track if spectator state has been detected
    spectator_mode_detected = False

    while running:
        try:
            # Use protocol's receive_packet instead of readline
            valid, header, payload = receive_packet(sock)
            
            if not valid:
                print("\n[ERROR] Server sent corrupted data. Please restart the client to reconnect.")
                if current_username:
                    print(f"[INFO] Your username '{current_username}' was saved for reconnection.")
                    save_connection_info(current_username)
                running = False
                break
                
            if payload is None:
                print("\n[ERROR] Server disconnected. Please restart the client to reconnect.")
                if current_username:
                    print(f"[INFO] Your username '{current_username}' was saved for reconnection.")
                    save_connection_info(current_username)
                running = False
                break
            
            magic, seq, packet_type, data_len = header
            payload_str = payload.decode() if isinstance(payload, bytes) else payload
            
            # Check for messages that indicate spectator mode
            if not spectator_mode_detected and packet_type == PACKET_TYPE_CHAT:
                if "spectating" in payload_str.lower() or "spectator" in payload_str.lower():
                    is_spectator = True
                    spectator_mode_detected = True
                    print("\n[INFO] You are in spectator mode. You can observe the game but cannot participate.")
                    print("[INFO] Type 'quit' to leave spectator mode.")
            
            # Process different packet types
            if packet_type == PACKET_TYPE_BOARD_UPDATE:
                print("\n" + payload_str)
            elif packet_type == PACKET_TYPE_GAME_START:
                print(f"\n[GAME START] {payload_str}")
            elif packet_type == PACKET_TYPE_GAME_END:
                print(f"\n[GAME END] {payload_str}")
                # Game is ending normally, remove connection file
                if current_username:
                    try:
                        os.remove(get_connection_file(current_username))
                        print("[DEBUG] Removed connection file as game ended normally")
                    except:
                        pass
            elif packet_type == PACKET_TYPE_ERROR:
                print(f"\n[ERROR] {payload_str}")
                if "timeout" in payload_str.lower() or "timed out" in payload_str.lower():
                    print("[ATTENTION] You have timed out! Please respond to avoid forfeiting your turn in future.")
                # Save connection info if error indicates disconnection
                if "disconnected" in payload_str.lower() or "connection lost" in payload_str.lower():
                    if current_username:
                        print(f"[INFO] Your username '{current_username}' was saved for reconnection.")
                        save_connection_info(current_username)
            elif packet_type == PACKET_TYPE_RECONNECT:
                print(f"\n[RECONNECTED] {payload_str}")
                # Mark as active since reconnection was successful
                if current_username:
                    mark_connection_active(current_username)
            elif packet_type == PACKET_TYPE_HEARTBEAT:
                # Respond to heartbeat with ACK to maintain connection
                print("[DEBUG] Received heartbeat, sending ACK")
                send_packet(sock, PACKET_TYPE_ACK, b'')
            elif packet_type == PACKET_TYPE_CHAT:
                # Regular message (chat, etc.)
                # Check if it's a chat message from another user
                if payload_str.startswith("[CHAT]"):
                    # Format and display chat message prominently
                    # Check if it's a spectator message (from Spectator@IP)
                    if "Spectator@" in payload_str:
                        # Extract the username part (remove IP address)
                        parts = payload_str.split(":", 1)
                        if len(parts) == 2:
                            spectator_info = parts[0].strip()
                            message = parts[1].strip()
                            # Extract username (could be just a number or identifier)
                            if "@" in spectator_info:
                                # Split to get the raw spectator username
                                username = spectator_info.split("@")[1].split(":")[0]
                                # Format as requested: "Username (spectator): message"
                                formatted_message = f"{username} (spectator): {message}"
                                print(f"\n{formatted_message}")
                            else:
                                # Fallback if format is unexpected
                                print(f"\n{payload_str}")
                        else:
                            # Fallback if format is unexpected
                            print(f"\n{payload_str}")
                    else:
                        # Regular chat message
                        print(f"\n{payload_str}")
                    print(">> ", end="", flush=True)  # Redisplay prompt
                else:
                    # Other server message
                    print(payload_str)
            else:
                print(f"[DEBUG] Unhandled packet type: {get_packet_type_name(packet_type)}")
                print(payload_str)
                
        except ConnectionResetError:
            print("\n[ERROR] Connection to server was reset. Please restart the client to reconnect.")
            if current_username:
                print(f"[INFO] Your username '{current_username}' was saved for reconnection.")
                save_connection_info(current_username)
            running = False
            break
        except BrokenPipeError:
            print("\n[ERROR] Connection to server was broken. Please restart the client to reconnect.")
            if current_username:
                print(f"[INFO] Your username '{current_username}' was saved for reconnection.")
                save_connection_info(current_username)
            running = False
            break
        except Exception as e:
            print(f"\n[ERROR] Error receiving from server: {e}")
            if current_username:
                print(f"[INFO] Your username '{current_username}' was saved for reconnection.")
                save_connection_info(current_username)
            running = False
            break

def main():
    global running, current_username, is_spectator

    print("[INFO] Connecting to Battleship server...")
    try:
        # Check for recent connections
        recent_connections = check_any_recent_connections()
        
        # Ask user for username
        username = ""
        
        # Only prompt for reconnection if there are recent connections
        if recent_connections:
            print("\n[INFO] Recent disconnection(s) found:")
            for i, (username, elapsed) in enumerate(recent_connections):
                print(f"  {i+1}. {username} ({elapsed:.1f} seconds ago)")
                
            reconnect_choice = input("\nWould you like to reconnect? (Enter number to reconnect, or 'n' for a new connection): ").strip().lower()
            
            if reconnect_choice.isdigit() and 1 <= int(reconnect_choice) <= len(recent_connections):
                # User selected a specific username to reconnect with
                username = recent_connections[int(reconnect_choice)-1][0]
                print(f"[INFO] Reconnecting as '{username}'...")
            else:
                # User wants a new connection, delete all recent connection files
                for username_to_delete, _ in recent_connections:
                    try:
                        os.remove(get_connection_file(username_to_delete))
                        print(f"[DEBUG] Deleted connection file for '{username_to_delete}'")
                    except:
                        pass
                
                # Ask for a new username
                username = ""
                while not username:
                    username = input("Enter your username: ").strip()
                    if not username:
                        print("[ERROR] Username cannot be empty. Please try again.")
        else:
            # No recent connections, ask for a username
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

            # Initially save connection info, but mark as active (not disconnected)
            save_connection_info(username)
            mark_connection_active(username)  # Mark as active since connected successfully
            
            # Start a thread for receiving messages
            receive_thread = threading.Thread(target=receive_messages, args=(s,))
            receive_thread.daemon = True # This ensures the thread will exit when the main thread exits
            receive_thread.start()

            try:
                # Display helpful information about expected client state
                print("\n[INFO] Waiting for server response...")
                print("[INFO] You may be placed as a player or spectator depending on server status.")
                print("[INFO] Type any message to chat with other players and spectators.")
                print("[INFO] Type 'quit' at any time to exit.")
                
                # Small delay to let initial messages come in
                time.sleep(1)

                # Main thread handles user input
                while running:
                    try:
                        user_input = input(">> ")

                        if user_input.lower() == 'quit':
                            print("[INFO] Quitting the game...")
                            send_packet(s, PACKET_TYPE_DISCONNECT, "Quit requested by user")
                            # Remove connection file when quitting deliberately
                            try:
                                os.remove(get_connection_file(current_username))
                                print("[DEBUG] Removed connection file after quit command")
                            except:
                                pass
                            running = False
                            break
                        
                        # Send user input to server using the protocol
                        # Determine packet type based on input
                        if len(user_input) == 0:
                            # Skip empty input
                            continue
                        elif user_input.lower().startswith("fire ") or (len(user_input) <= 3 and any(c.isalpha() and c.upper() in "ABCDEFGHIJ" for c in user_input)):
                            # This is a game move (fire command or coordinate)
                            if not send_packet(s, PACKET_TYPE_MOVE, user_input):
                                print("[ERROR] Failed to send move to server")
                        elif user_input.upper() in ["H", "V", "M", "R", "Y", "N", "YES", "NO"]:
                            # Ship placement or yes/no response
                            if not send_packet(s, PACKET_TYPE_MOVE, user_input):
                                print("[ERROR] Failed to send move to server")
                        else:
                            # Treat as chat message
                            if not send_packet(s, PACKET_TYPE_CHAT, user_input):
                                print("[ERROR] Failed to send message to server")
                            # If user is spectator - put in front of chat messages
                            elif is_spectator:
                                # Format consistent with received messages
                                username = current_username.split('@')[0] if '@' in current_username else current_username
                                print(f"\n{username} (spectator): {user_input}")
                                print(">> ", end="", flush=True)  # Redisplay prompt
                            else:
                                # Echo own chat message locally
                                print(f"\n{current_username}: {user_input}")
                                print(">> ", end="", flush=True)  # Redisplay prompt

                    except KeyboardInterrupt:
                        print("\n[INFO] Client exiting due to keyboard interrupt.")
                        send_packet(s, PACKET_TYPE_DISCONNECT, "Keyboard interrupt")
                        # Remove connection file on clean exit
                        try:
                            os.remove(get_connection_file(current_username))
                            print("[DEBUG] Removed connection file after keyboard interrupt")
                        except:
                            pass
                        running = False
                        break
                    except EOFError:
                        print("\n[INFO] End of input reached. Exiting...")
                        send_packet(s, PACKET_TYPE_DISCONNECT, "EOF reached")
                        try:
                            os.remove(get_connection_file(current_username))
                            print("[DEBUG] Removed connection file after EOF")
                        except:
                            pass
                        running = False
                        break
                    except Exception as e:
                        print(f"\n[ERROR] Unexpected error: {e}")
                        running = False
                        break

            except KeyboardInterrupt:
                print("\n[INFO] Client exiting due to keyboard interrupt.")
                send_packet(s, PACKET_TYPE_DISCONNECT, "Keyboard interrupt")
                try:
                    os.remove(get_connection_file(current_username))
                    print("[DEBUG] Removed connection file after keyboard interrupt")
                except:
                    pass
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