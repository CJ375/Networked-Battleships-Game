"""
server.py

This module implements a Battleship game server that manages game sessions, player connections,
and game state. It supports multiple concurrent games, spectator mode, and player reconnection.
The server deals with game logic and coordinates communication between players.
"""

import socket
import threading
import traceback
import time
import queue
import select
import random
import json
from battleship import run_two_player_game, PlayerDisconnectedError
from protocol import (
    receive_packet, send_packet, 
    PACKET_TYPE_USERNAME, PACKET_TYPE_MOVE, PACKET_TYPE_CHAT,
    PACKET_TYPE_DISCONNECT, PACKET_TYPE_RECONNECT, PACKET_TYPE_HEARTBEAT,
    PACKET_TYPE_GAME_START, PACKET_TYPE_BOARD_UPDATE, PACKET_TYPE_GAME_END,
    PACKET_TYPE_ERROR, PACKET_TYPE_ACK, get_packet_type_name
)

# Server configuration and timeouts
HOST = '127.0.0.1'
PORT = 5001
CONNECTION_TIMEOUT = 60  # seconds to wait for a connection
HEARTBEAT_INTERVAL = 30  # seconds between heartbeat checks
MOVE_TIMEOUT = 30  # seconds a player has to make a move
RECONNECT_TIMEOUT = 60  # seconds a player can reconnect after disconnection

# Game state management
game_in_progress = False
game_lock = threading.Lock()
waiting_players = queue.Queue()
waiting_players_lock = threading.Lock()
current_game_spectators = []
spectators_lock = threading.Lock()

# Player tracking and connection management
active_usernames = {}
active_usernames_lock = threading.Lock()
disconnected_players = {}
disconnected_players_lock = threading.Lock()
player_connections = {}
player_connections_lock = threading.Lock()
current_games = {}
current_games_lock = threading.Lock()

# Game state representation
from battleship import BOARD_SIZE
class DummyGame:
    """Represents a placeholder game state when no active game is in progress."""
    def __init__(self):
        self.board_size = BOARD_SIZE
        self.player1 = "Waiting for players"
        self.player2 = "Waiting for players"
        self.current_turn = None
        self.game_state = "waiting"
        self.last_move = None
        self.last_move_result = None

class RealGame:
    """Represents an active game session between two players."""
    def __init__(self, player1, player2, game_id):
        self.board_size = BOARD_SIZE
        self.player1 = player1
        self.player2 = player2
        self.current_turn = player1
        self.game_state = "setup"
        self.last_move = None
        self.last_move_result = None
        self.game_id = game_id

# Initialize with a dummy game
current_game = DummyGame()

def broadcast_chat_message(sender_username, message):
    """Broadcasts a chat message to all connected players and spectators."""
    chat_msg = f"[CHAT] {sender_username}: {message}"
    print(f"[INFO] Broadcasting chat: {chat_msg}")
    
    recipients = []
    
    with active_usernames_lock:
        for username, conn in active_usernames.items():
            recipients.append((username, conn))
    
    with spectators_lock:
        for conn in current_game_spectators:
            recipients.append((None, conn))
    
    for username, conn in recipients:
        try:
            if username == sender_username:
                continue
            send_packet(conn, PACKET_TYPE_CHAT, chat_msg)
        except:
            pass

def _is_connection_alive(conn, username_for_log):
    """Checks if a given connection is alive using heartbeat mechanism."""
    try:
        if send_packet(conn, PACKET_TYPE_HEARTBEAT, ""):
            valid_ack, header_ack, _ = receive_packet(conn, timeout=5.0)
            if valid_ack and header_ack and header_ack[2] == PACKET_TYPE_ACK:
                print(f"[INFO] Heartbeat-ACK received from '{username_for_log}'. Connection is alive.")
                return True
            else:
                ack_type = get_packet_type_name(header_ack[2]) if header_ack else 'N/A'
                print(f"[INFO] Heartbeat sent to '{username_for_log}', but no/invalid ACK received (valid={valid_ack}, type={ack_type}). Connection stale.")
                return False
        else:
            print(f"[INFO] Heartbeat send to '{username_for_log}' failed. Connection stale.")
            return False
    except socket.timeout:
        print(f"[INFO] Socket timeout waiting for ACK from '{username_for_log}'. Connection stale.")
        return False
    except (socket.error, BrokenPipeError, ConnectionResetError) as e_sock:
        print(f"[INFO] Socket error during Heartbeat-ACK with '{username_for_log}': {e_sock}. Connection stale.")
        return False
    except Exception as e_other:
        print(f"[INFO] Unexpected error during Heartbeat-ACK with '{username_for_log}': {e_other}. Connection stale.")
        return False

def _send_spectator_message(conn, packet_type, payload, failure_context_msg):
    """Sends a packet to a spectator and logs any failures."""
    try:
        if not send_packet(conn, packet_type, payload):
            print(f"[INFO] Failed to send spectator message ({failure_context_msg})")
            return False
        return True
    except (ConnectionResetError, BrokenPipeError) as e:
        print(f"[INFO] Connection error sending spectator message ({failure_context_msg}): {e}")
        return False
    except Exception as e:
        print(f"[INFO] Unexpected error sending spectator message ({failure_context_msg}): {e}")
        return False

class ProtocolAdapter:
    """Adapts the game protocol to handle different types of packets and messages."""
    
    def __init__(self, conn, username):
        self.conn = conn
        self.username = username
        self.buffer = []
        self.last_packet_type = None
        self.grid_mode = False
        
    def readline(self):
        """Reads a line for game logic from incoming packets,
           handling other packet types like CHAT appropriately.
           This method should only return when a game-relevant input is received.
        """

        while True:
            valid, header, payload = receive_packet(self.conn, timeout=MOVE_TIMEOUT)

            if not valid or payload is None:
                log_message = f"readline for {self.username}: "
                if not valid and header is None and payload is None:
                    log_message += "Receive_packet indicated disconnect/timeout."
                else:
                    log_message += f"Failed to receive valid packet or payload. Valid: {valid}, Header: {header is not None}, Payload: {payload is not None}"
                print(f"[ADAPTER INFO] {log_message}")
                raise PlayerDisconnectedError(self.username, None)

            payload_str = payload.decode() if isinstance(payload, bytes) else payload
            _magic, _seq, packet_type, _data_len = header
            self.last_packet_type = packet_type

            print(f"[ADAPTER DEBUG] {self.username} - Received Packet Type: {get_packet_type_name(packet_type)}, Payload: '{payload_str.strip()}'")

            if packet_type == PACKET_TYPE_MOVE:
                print(f"[ADAPTER INFO] {self.username} - Returning MOVE: '{payload_str.strip()}' to game logic.")
                return payload_str.strip()

            elif packet_type == PACKET_TYPE_CHAT:
                stripped_payload = payload_str.strip().upper()
                game_command_chats = ['Y', 'N', 'YES', 'NO', 'QUIT']

                if stripped_payload in game_command_chats:
                    print(f"[ADAPTER INFO] {self.username} - CHAT interpreted as game command: '{stripped_payload}'. Returning to game logic.")
                    return stripped_payload
                else:
                    print(f"[ADAPTER INFO] {self.username} - Broadcasting CHAT: '{payload_str.strip()}' and continuing to listen for game input.")
                    broadcast_chat_message(self.username, payload_str.strip())
                    continue

            elif packet_type == PACKET_TYPE_DISCONNECT:
                print(f"[ADAPTER INFO] {self.username} - Received DISCONNECT signal during readline.")
                raise PlayerDisconnectedError(self.username, None)

            elif packet_type == PACKET_TYPE_HEARTBEAT:
                print(f"[ADAPTER WARNING] {self.username} - Received unexpected HEARTBEAT. Sending ACK and continuing.")
                if not send_packet(self.conn, PACKET_TYPE_ACK, b""):
                    raise ConnectionResetError(f"Failed to send ACK for unexpected HEARTBEAT from {self.username}")
                continue

            elif packet_type == PACKET_TYPE_ACK:
                print(f"[ADAPTER WARNING] {self.username} - Received unexpected ACK. Ignoring and continuing.")
                continue

            else:
                print(f"[ADAPTER WARNING] {self.username} - Received unhandled packet type {get_packet_type_name(packet_type)} in readline. Content: '{payload_str.strip()}'. Ignoring and continuing to listen for game input.")
                continue
            
    def write(self, msg):
        """Writes a message to be sent as a packet."""
        player_name_for_error = self.username if self.username else "UnknownPlayerAdapterUser"

        if msg.strip() == "Your Grid:" or msg.strip() == "Opponent's Grid:" or msg.strip() == "SPECTATOR_GRID":
            if self.grid_mode and self.buffer:
                 if not self.flush():
                    raise PlayerDisconnectedError(player_name_for_error, None) 
            self.grid_mode = True
            self.buffer = [msg]
        elif self.grid_mode and (msg.strip() == "" or msg == "\n"):
            self.grid_mode = False
            self.buffer.append(msg)
            if not self.flush():
                raise PlayerDisconnectedError(player_name_for_error, None)
        elif self.grid_mode:
            self.buffer.append(msg)
        else:
            if not send_packet(self.conn, PACKET_TYPE_CHAT, msg.strip()):
                print(f"[ADAPTER ERROR] send_packet failed for CHAT in write() for {self.username}")
                raise PlayerDisconnectedError(self.username, None)
        
        return len(msg)
        
    def flush(self):
        """Sends any buffered grid updates."""
        player_name_for_error = self.username if self.username else "UnknownPlayerAdapterUser"
        if self.buffer:
            grid_msg_to_send = ''.join(self.buffer)
            self.buffer = [] 
            self.grid_mode = False 

            if not send_packet(self.conn, PACKET_TYPE_BOARD_UPDATE, grid_msg_to_send):
                print(f"[ADAPTER ERROR] send_packet failed for BOARD_UPDATE in flush() for {self.username}")
                raise PlayerDisconnectedError(self.username, None)
        return True

def handle_player_disconnect(player_conn, player_name):
    """Handles a player disconnection during gameplay."""
    with disconnected_players_lock:
        disconnected_players[player_name] = {
            'disconnect_time': time.time(),
        }
        print(f"[INFO] {player_name} marked as disconnected. Reconnection window: {RECONNECT_TIMEOUT} seconds")
    
    with active_usernames_lock:
        if player_name in active_usernames:
            del active_usernames[player_name]
            print(f"[INFO] {player_name} removed from active usernames")
    
    try:
        send_packet(player_conn, PACKET_TYPE_ERROR, f"Connection lost. You have been disconnected from the game.")
    except:
        pass
        
    try:
        player_conn.close()
    except:
        pass

def ask_play_again(player_conn):
    """Asks a player if they want to play another game."""
    try:
        send_packet(player_conn, PACKET_TYPE_CHAT, "Do you want to play again? (Y/N):")
        valid, header, payload = receive_packet(player_conn, timeout=30)
        
        if valid and payload:
            payload_str = payload.decode() if isinstance(payload, bytes) else payload
            return payload_str.upper() == 'Y' or payload_str.upper() == 'YES'
        return False
    except Exception as e:
        print(f"Error asking player to play again: {e}")
        return False

def handle_waiting_player(conn, addr, username, stop_event):
    """Manages a player in the waiting lobby until a game starts or they quit."""
    print(f"[INFO] {username} entered waiting lobby.")
    is_active_player = True

    try:
        send_packet(conn, PACKET_TYPE_CHAT, "\nYou are in the waiting lobby. Waiting for another player...")
        send_packet(conn, PACKET_TYPE_CHAT, "Type 'quit' to leave the waiting lobby, or send messages to chat with others.")
        
        last_status_update_time = time.time()

        while not stop_event.is_set():
            if stop_event.wait(timeout=0.2):
                is_active_player = False
                break

            try:
                valid, header, payload = receive_packet(conn, timeout=2.0) 
                
                if stop_event.is_set():
                    is_active_player = False
                    break

                if valid and payload:
                    payload_str = payload.decode() if isinstance(payload, bytes) else payload
                    _, _, packet_type, _ = header
                    
                    if packet_type == PACKET_TYPE_CHAT:
                        if payload_str.lower() == 'quit':
                            print(f"[INFO] {username} has chosen to quit the waiting lobby.")
                            send_packet(conn, PACKET_TYPE_CHAT, "You have left the waiting lobby.")
                            return
                        else:
                            broadcast_chat_message(username, payload_str)
                    
                    elif packet_type == PACKET_TYPE_DISCONNECT:
                        print(f"[INFO] {username} has disconnected from the waiting lobby (received DISCONNECT).")
                        return
                    elif packet_type == PACKET_TYPE_HEARTBEAT:
                        send_packet(conn, PACKET_TYPE_ACK, b'')
                elif payload is None and not valid and header is None:
                    pass
                elif not valid and header is not None:
                    print(f"[DEBUG] Corrupted packet received from waiting player {username}.")
                
                if time.time() - last_status_update_time > 20:
                    if stop_event.is_set(): break
                    send_packet(conn, PACKET_TYPE_CHAT, "Still waiting for a game...")
                    send_packet(conn, PACKET_TYPE_HEARTBEAT, "")
                    last_status_update_time = time.time()
                
            except socket.timeout: 
                continue 
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                print(f"[INFO] {username} disconnected from waiting lobby at {addr}: {e}")
                return
            except Exception as e:
                print(f"[ERROR] Unexpected error in handle_waiting_player for {username}: {e}")
                return
        
        print(f"[DEBUG] handle_waiting_player for {username} loop finished. stop_event set: {stop_event.is_set()}")

    except Exception as e:
        print(f"[ERROR] Error during setup of waiting player {username}: {e}")
    finally:
        print(f"[DEBUG] handle_waiting_player for {username} finalizing. is_active_player={is_active_player}, stop_event_set={stop_event.is_set()}")
        if is_active_player and not stop_event.is_set():
            with active_usernames_lock:
                if username in active_usernames and active_usernames[username] == conn:
                    print(f"[DEBUG] Cleaning up {username} from active_usernames in handle_waiting_player.")
                    del active_usernames[username]
            try:
                conn.close()
            except: pass
        elif not is_active_player:
             print(f"[DEBUG] {username} is moving to a game, active_usernames not cleaned by handle_waiting_player.")

def handle_spectator(conn, addr, game):
    """Manages a spectator connection, providing game updates and chat functionality."""
    spectator_username = None
    with active_usernames_lock:
        for username, conn_obj in active_usernames.items():
            if conn_obj == conn:
                spectator_username = username
                break
    
    if not spectator_username:
        spectator_username = f"Spectator@{addr[0]}:{addr[1]}"
        print(f"[WARNING] Could not find username for spectator {addr}, using fallback name")
    
    try:
        if not _send_spectator_message(conn, PACKET_TYPE_CHAT, "\nWelcome! You are now spectating a Battleship game.", "welcome"):
            return
            
        if not _send_spectator_message(conn, PACKET_TYPE_CHAT, "You will see all game updates but cannot participate in the game.", "no participation info"):
            return
            
        if not _send_spectator_message(conn, PACKET_TYPE_CHAT, "Type 'quit' to stop spectating. You can send chat messages that will be seen by all players and spectators.", "quit instructions"):
            return
        
        game_state_message = f"\nCurrent Game Status:\n"
        game_state_message += f"Player 1: {game.player1}\n"
        game_state_message += f"Player 2: {game.player2}\n"
        game_state_message += f"Game State: {game.game_state}\n"
        
        if game.current_turn:
            game_state_message += f"Current Turn: {game.current_turn}\n"
        else:
            game_state_message += "Waiting for game to start...\n"
            
        if not _send_spectator_message(conn, PACKET_TYPE_CHAT, game_state_message, "initial game state"):
            return

        if isinstance(game, RealGame) and game.player1 and game.player2 and game.player1 != "Waiting for players":
            player_info_msg = f"SPECTATOR_PLAYER_NAMES:P1={game.player1},P2={game.player2}"
            if not _send_spectator_message(conn, PACKET_TYPE_CHAT, player_info_msg, "player names"):
                print(f"[WARNING] Failed to send SPECTATOR_PLAYER_NAMES to {spectator_username}")

        with spectators_lock:
            if conn not in current_game_spectators:
                current_game_spectators.append(conn)
                print(f"[DEBUG] Added spectator to list. Total spectators: {len(current_game_spectators)}")

        broadcast_chat_message("SERVER", f"A new spectator has joined to watch the game")

        conn.settimeout(30)
        
        last_heartbeat = time.time()
        heartbeat_interval = 15
        socket_errors_count = 0
        max_socket_errors = 5  # Allow a few socket errors before disconnecting
        
        while True:
            try:
                current_time = time.time()
                
                # Don't send heartbeats too frequently if there have been socket errors
                if current_time - last_heartbeat >= heartbeat_interval and socket_errors_count < max_socket_errors:
                    try:
                        if not _send_spectator_message(conn, PACKET_TYPE_HEARTBEAT, b'', "heartbeat send"):
                            socket_errors_count += 1
                            print(f"[DEBUG] Heartbeat to spectator {spectator_username} failed. Error count: {socket_errors_count}")
                            if socket_errors_count >= max_socket_errors:
                                print(f"[DEBUG] Too many socket errors for spectator {spectator_username}. Disconnecting.")
                                break
                        else:
                            # Reset error count on successful heartbeat
                            socket_errors_count = 0
                        last_heartbeat = current_time
                    except Exception as e:
                        print(f"[DEBUG] Error sending heartbeat to spectator {spectator_username}: {e}")
                        socket_errors_count += 1
                        if socket_errors_count >= max_socket_errors:
                            break
                
                try:
                    is_valid, header, payload = receive_packet(conn, timeout=1.0)
                    
                    if not is_valid and header is not None:
                        print("[DEBUG] Received invalid packet from spectator")
                        continue
                        
                    if header is None:
                        continue
                        
                    magic, seq, ptype, dlen = header
                    print(f"[DEBUG] Received packet from spectator: type={get_packet_type_name(ptype)}")
                    
                    if ptype == PACKET_TYPE_HEARTBEAT:
                        print(f"[DEBUG] Received heartbeat from spectator {spectator_username}")
                        try:
                            if not _send_spectator_message(conn, PACKET_TYPE_ACK, b'', "heartbeat ACK"):
                                socket_errors_count += 1
                                if socket_errors_count >= max_socket_errors:
                                    break
                        except Exception as e:
                            print(f"[DEBUG] Error sending ACK to spectator heartbeat: {e}")
                            socket_errors_count += 1
                            if socket_errors_count >= max_socket_errors:
                                break
                    elif ptype == PACKET_TYPE_ACK:
                        print(f"[DEBUG] Received ACK from spectator {spectator_username}")
                        socket_errors_count = 0  # Successfully received an ACK, reset error count
                        continue
                    elif ptype == PACKET_TYPE_CHAT:
                        payload_str = payload.decode() if isinstance(payload, bytes) else payload
                        if payload_str.lower() == 'quit':
                            print(f"[DEBUG] Spectator {addr} requested to quit")
                            _send_spectator_message(conn, PACKET_TYPE_CHAT, "You have left the spectator mode. Goodbye!", "quit confirmation")
                            break
                        else:
                            try:
                                broadcast_chat_message(spectator_username, payload_str)
                                socket_errors_count = 0  # Successfully processed a chat, reset error count
                            except Exception as e:
                                print(f"[DEBUG] Error broadcasting spectator chat: {e}")
                                socket_errors_count += 1
                                if socket_errors_count >= max_socket_errors:
                                    break
                    elif ptype == PACKET_TYPE_MOVE:
                        _send_spectator_message(conn, PACKET_TYPE_CHAT, f"As a spectator, you cannot make moves. Type 'quit' to leave, or send chat messages.", "move restriction")
                    else:
                        print(f"[DEBUG] Unexpected packet type from spectator {spectator_username}: {get_packet_type_name(ptype)}")
                        _send_spectator_message(conn, PACKET_TYPE_CHAT, "As a spectator, you can use 'quit' to leave or send chat messages.", "help message")
                        continue
                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"[DEBUG] Error handling spectator packet: {e}")
                    socket_errors_count += 1
                    if socket_errors_count >= max_socket_errors:
                        print(f"[DEBUG] Too many errors handling spectator packets. Disconnecting spectator {spectator_username}.")
                        break
                    continue
                    
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[DEBUG] Fatal error in spectator handler: {e}")
                socket_errors_count += 1
                if socket_errors_count >= max_socket_errors:
                    break
                continue
                
    except Exception as e:
        print(f"[DEBUG] Fatal error in spectator handler: {e}")
    finally:
        print(f"[DEBUG] Closing spectator connection from {addr}")
        with spectators_lock:
            if conn in current_game_spectators:
                current_game_spectators.remove(conn)
                print(f"[DEBUG] Removed spectator from list. Remaining spectators: {len(current_game_spectators)}")
                
        broadcast_chat_message("SERVER", f"A spectator has left the game")
        try:
            conn.close()
        except Exception as e:
            print(f"[DEBUG] Error closing spectator connection: {e}")

def notify_spectators(message):
    """Sends a message to all connected spectators."""
    with spectators_lock:
        spectators_to_remove = []
        for conn in list(current_game_spectators):
            try:
                if not send_packet(conn, PACKET_TYPE_BOARD_UPDATE, message):
                    print(f"[INFO] Failed to send board update to spectator. Adding to removal list.")
                    spectators_to_remove.append(conn)
            except (ConnectionResetError, BrokenPipeError) as e:
                print(f"[INFO] Connection error sending board update to spectator: {e}. Adding to removal list.")
                spectators_to_remove.append(conn)
            except Exception as e:
                print(f"[INFO] Unexpected error sending board update to spectator: {e}. Adding to removal list.")
                spectators_to_remove.append(conn)
        
        # Remove any broken connections
        for conn in spectators_to_remove:
            if conn in current_game_spectators:
                current_game_spectators.remove(conn)
                try:
                    conn.close()
                except:
                    pass

def send_event_to_spectators(event_message):
    """Sends a CHAT message to all connected spectators for game events."""
    with spectators_lock:
        spectators_to_remove = []
        for conn in list(current_game_spectators):
            try:
                # Prefixing for clarity on the client side, though client can format as it sees fit
                if not send_packet(conn, PACKET_TYPE_CHAT, f"[GAME EVENT] {event_message}"):
                    print(f"[INFO] Failed to send event to spectator. Adding to removal list.")
                    spectators_to_remove.append(conn)
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                print(f"[INFO] Socket error sending event to spectator: {e}. Adding to removal list.")
                spectators_to_remove.append(conn)
            except Exception as e:
                print(f"[INFO] Unexpected error sending event to spectator: {e}. Adding to removal list.")
                spectators_to_remove.append(conn)
        
        # Clean up broken connections
        for conn in spectators_to_remove:
            if conn in current_game_spectators:
                current_game_spectators.remove(conn)
                try:
                    conn.close()
                except:
                    pass

def handle_game_session(player1_conn, player2_conn, player1_addr, player2_addr, player1_username, player2_username, game_id):
    """Manages a game session between two players, handling gameplay, disconnections, and rematches."""
    global game_in_progress, current_game_spectators, disconnected_players, current_game, active_usernames
    
    original_player1_conn = player1_conn
    original_player2_conn = player2_conn

    player1_adapter = ProtocolAdapter(player1_conn, player1_username)
    player2_adapter = ProtocolAdapter(player2_conn, player2_username)
    
    try:
        play_again = True
        resumed_game_state = None

        while play_again:
            current_p1_board_state = None
            current_p2_board_state = None
            next_player_for_turn = None
            
            game_ended_due_to_disconnect = False

            if resumed_game_state:
                print(f"[GAME SESSION {game_id}] Attempting to resume game with loaded state.")
                p1_state_from_save = resumed_game_state.get('player1_board_state')
                p2_state_from_save = resumed_game_state.get('player2_board_state')
                next_player_from_save = resumed_game_state.get('next_turn_username')

                if resumed_game_state.get('player1_of_state') == player1_username:
                    current_p1_board_state = p1_state_from_save
                    current_p2_board_state = p2_state_from_save
                elif resumed_game_state.get('player2_of_state') == player1_username:
                    current_p1_board_state = p2_state_from_save
                    current_p2_board_state = p1_state_from_save
                else:
                    print(f"[GAME SESSION {game_id} WARNING] Could not map saved player states to current player usernames. P1_of_state='{resumed_game_state.get('player1_of_state')}', P2_of_state='{resumed_game_state.get('player2_of_state')}'. Current P1='{player1_username}'.")
                    current_p1_board_state = p1_state_from_save
                    current_p2_board_state = p2_state_from_save

                next_player_for_turn = next_player_from_save
                
                print(f"[GAME SESSION {game_id}] DEBUG: Resuming with: P1 Board State Present: {bool(current_p1_board_state)}, P2 Board State Present: {bool(current_p2_board_state)}, Next Turn: {next_player_for_turn}")

                with active_usernames_lock:
                    if player1_username in active_usernames:
                        new_p1_conn = active_usernames[player1_username]
                        if player1_adapter.conn != new_p1_conn:
                            print(f"[GAME SESSION {game_id}] Updating P1 adapter to new connection for {player1_username}")
                            player1_adapter.conn = new_p1_conn
                    else:
                        print(f"[GAME SESSION {game_id} WARNING] P1 {player1_username} not in active_usernames during resume setup!")

                    if player2_username in active_usernames:
                        new_p2_conn = active_usernames[player2_username]
                        if player2_adapter.conn != new_p2_conn:
                            print(f"[GAME SESSION {game_id}] Updating P2 adapter to new connection for {player2_username}")
                            player2_adapter.conn = new_p2_conn
                    else:
                        print(f"[GAME SESSION {game_id} WARNING] P2 {player2_username} not in active_usernames during resume setup (might be okay if they just reconnected).")

                resumed_game_state = None
            else:
                print(f"[GAME SESSION {game_id}] Starting a new game instance.")
                current_game.game_state = "starting"
                current_game.last_move = None
                current_game.last_move_result = None
                send_packet(player1_adapter.conn, PACKET_TYPE_GAME_START, f"Starting game against {player2_username}")
                send_packet(player2_adapter.conn, PACKET_TYPE_GAME_START, f"Starting game against {player1_username}")
                notify_spectators("A new game is starting!")

            try:
                run_two_player_game(
                    player1_adapter, player1_adapter, player2_adapter, player2_adapter,
                    notify_spectators,
                    send_event_to_spectators,
                    player1_username=player1_username, player2_username=player2_username,
                    initial_player1_board_state=current_p1_board_state,
                    initial_player2_board_state=current_p2_board_state,
                    initial_current_player_name=next_player_for_turn
                )
                current_game.game_state = "completed"

            except PlayerDisconnectedError as pde:
                game_ended_due_to_disconnect = True
                print(f"[GAME SESSION {game_id}] PlayerDisconnectedError: {pde.player_name} disconnected.")

                if pde.player_name == player1_username:
                    handle_player_disconnect(player1_adapter.conn, player1_username)
                else:
                    handle_player_disconnect(player2_adapter.conn, player2_username)
                
                disconnected_player_name = pde.player_name
                other_player_name = player2_username if disconnected_player_name == player1_username else player1_username
                other_player_adapter = player2_adapter if disconnected_player_name == player1_username else player1_adapter
                
                saved_state = None
                if pde.game_state:
                    try:
                        saved_state = {
                            'player1_of_state': player1_username,
                            'player2_of_state': player2_username,
                            'player1_board_state': pde.game_state.get('player1_board_state'),
                            'player2_board_state': pde.game_state.get('player2_board_state'),
                            'next_turn_username': pde.game_state.get('next_turn_username')
                        }
                    except AttributeError:
                        print(f"[GAME SESSION {game_id}] Warning: pde.game_state was present but not a dictionary. No detailed state saved.")
                        saved_state = None
                else:
                    print(f"[GAME SESSION {game_id}] Warning: PlayerDisconnectedError for {pde.player_name} did not contain detailed game_state. Game may not be resumable.")
                    saved_state = {
                        'player1_of_state': player1_username,
                        'player2_of_state': player2_username,
                        'player1_board_state': None,
                        'player2_board_state': None,
                        'next_turn_username': other_player_name
                    }

                print(f"[GAME SESSION {game_id} DEBUG] State being saved for {disconnected_player_name}: {json.dumps(saved_state, indent=2) if saved_state else 'None'}")

                with disconnected_players_lock:
                    player_disconnect_info = {
                        'disconnect_time': time.time(),
                        'opponent_username': other_player_name,
                        'game_id': game_id
                    }
                    if saved_state:
                        player_disconnect_info['game_state'] = saved_state
                    else:
                        print(f"[GAME SESSION {game_id}] No valid game_state object created; not saving detailed game state for {disconnected_player_name}.")

                    disconnected_players[disconnected_player_name] = player_disconnect_info
                
                print(f"[GAME SESSION {game_id}] Saved disconnect info for {disconnected_player_name}. Resumable state available: {bool(saved_state and saved_state.get('player1_board_state'))}")

                msg_for_other = f"\n{disconnected_player_name} has disconnected. Waiting {RECONNECT_TIMEOUT} seconds for reconnection..."
                msg_for_spectators = f"{disconnected_player_name} has disconnected. Waiting for reconnection..."
                try:
                    send_packet(other_player_adapter.conn, PACKET_TYPE_CHAT, msg_for_other)
                except Exception as e:
                    print(f"[GAME SESSION {game_id}] Error notifying {other_player_name} of disconnect: {e}")
                notify_spectators(msg_for_spectators)
                current_game.game_state = "interrupted_waiting_reconnect"

                reconnected_successfully = False
                wait_start_time = time.time()
                while time.time() - wait_start_time < RECONNECT_TIMEOUT:
                    with active_usernames_lock:
                        if disconnected_player_name in active_usernames:
                            print(f"[GAME SESSION {game_id}] {disconnected_player_name} appears in active_usernames. Attempting to resume.")
                            
                            with disconnected_players_lock:
                                if disconnected_player_name in disconnected_players and \
                                   disconnected_players[disconnected_player_name].get('game_id') == game_id:
                                    
                                    resumed_game_state = disconnected_players[disconnected_player_name]['game_state']
                                    del disconnected_players[disconnected_player_name]
                                    reconnected_successfully = True
                                    print(f"[GAME SESSION {game_id}] State retrieved for {disconnected_player_name}. Will resume game.")
                                else:
                                    print(f"[GAME SESSION {game_id}] {disconnected_player_name} reconnected, but no/mismatching game state found. Cannot resume this game.")
                                    reconnected_successfully = False
                            break 
                    
                    if int(time.time() - wait_start_time) % 10 == 0:
                        try:
                            send_packet(other_player_adapter.conn, PACKET_TYPE_CHAT, f"Still waiting for {disconnected_player_name} to reconnect... ({int(RECONNECT_TIMEOUT - (time.time() - wait_start_time))}s left)")
                        except: pass
                    time.sleep(1)

                if reconnected_successfully:
                    send_packet(other_player_adapter.conn, PACKET_TYPE_CHAT, f"{disconnected_player_name} has reconnected. Resuming game.")
                    notify_spectators(f"{disconnected_player_name} has reconnected. Resuming game.")
                    with active_usernames_lock:
                        new_conn_for_reconnected = active_usernames[disconnected_player_name]
                    if disconnected_player_name == player1_username:
                        player1_adapter.conn = new_conn_for_reconnected
                        send_packet(player1_adapter.conn, PACKET_TYPE_RECONNECT, "Successfully reconnected to your game.")
                    else:
                        player2_adapter.conn = new_conn_for_reconnected
                        send_packet(player2_adapter.conn, PACKET_TYPE_RECONNECT, "Successfully reconnected to your game.")
                    current_game.game_state = "in_progress"
                    continue

                else:
                    print(f"[GAME SESSION {game_id}] {disconnected_player_name} did not reconnect. {other_player_name} wins by default.")
                    current_game.game_state = "completed_by_forfeit"
                    current_game.last_move_result = f"{other_player_name} wins by default (opponent disconnect)."
                    try:
                        send_packet(other_player_adapter.conn, PACKET_TYPE_GAME_END, f"{disconnected_player_name} did not reconnect. You win by default!")
                    except Exception as e:
                         print(f"[GAME SESSION {game_id}] Error notifying {other_player_name} of win by default: {e}")
                    notify_spectators(f"{disconnected_player_name} did not reconnect. {other_player_name} wins by default.")
                    play_again = False
                    break

            if not game_ended_due_to_disconnect:
                print(f"[GAME SESSION {game_id}] Game instance finished normally.")
                send_packet(player1_adapter.conn, PACKET_TYPE_CHAT, "Game over! Please wait...")
                send_packet(player2_adapter.conn, PACKET_TYPE_CHAT, "Game over! Please wait...")
                time.sleep(2)

                player1_wants_rematch = ask_play_again(player1_adapter.conn)
                player2_wants_rematch = ask_play_again(player2_adapter.conn)

                if player1_wants_rematch and player2_wants_rematch:
                    send_packet(player1_adapter.conn, PACKET_TYPE_CHAT, "Both players want a rematch! Starting new game...")
                    send_packet(player2_adapter.conn, PACKET_TYPE_CHAT, "Both players want a rematch! Starting new game...")
                    notify_spectators("Players agreed to a rematch!")
                    play_again = True
                else:
                    with spectators_lock:
                        available_spectators = list(current_game_spectators)
                    
                    if not player1_wants_rematch and not player2_wants_rematch:
                        send_packet(player1_adapter.conn, PACKET_TYPE_GAME_END, "You declined rematch. Session ending.")
                        send_packet(player2_adapter.conn, PACKET_TYPE_GAME_END, "You declined rematch. Session ending.")
                        
                        if len(available_spectators) >= 2:
                            notify_spectators(f"Both players declined rematch. Starting a new game with the first two spectators!")
                            
                            spec1_conn = available_spectators[0]
                            spec2_conn = available_spectators[1]
                            
                            with spectators_lock:
                                current_game_spectators.remove(spec1_conn)
                                current_game_spectators.remove(spec2_conn)
                            
                            send_packet(spec1_conn, PACKET_TYPE_CHAT, "Please reconnect with a username to join the game as a player.")
                            send_packet(spec2_conn, PACKET_TYPE_CHAT, "Please reconnect with a username to join the game as a player.")
                            
                            try:
                                spec1_conn.close()
                            except: pass
                            try:
                                spec2_conn.close()
                            except: pass
                        else:
                            notify_spectators(f"Both players declined rematch and not enough spectators for a new game. Game session is ending.")
                        
                        play_again = False
                    
                    elif player1_wants_rematch or player2_wants_rematch:
                        staying_player = player1_username if player1_wants_rematch else player2_username
                        leaving_player = player2_username if player1_wants_rematch else player1_username
                        staying_conn = player1_adapter.conn if player1_wants_rematch else player2_adapter.conn
                        staying_adapter = player1_adapter if player1_wants_rematch else player2_adapter
                        leaving_conn = player2_adapter.conn if player1_wants_rematch else player1_adapter.conn

                        send_packet(leaving_conn, PACKET_TYPE_GAME_END, "You declined rematch. Session ending.")
                        
                        if available_spectators:
                            spec_conn = available_spectators[0]
                            
                            with spectators_lock:
                                current_game_spectators.remove(spec_conn)
                            
                            send_packet(staying_conn, PACKET_TYPE_CHAT, f"{leaving_player} declined rematch. You'll play against a spectator instead.")
                            send_packet(spec_conn, PACKET_TYPE_CHAT, "Please reconnect with a username to join the game as a player.")
                            notify_spectators(f"{leaving_player} declined rematch. {staying_player} will play against a new opponent from spectators.")
                            
                            try:
                                spec_conn.close()
                            except: pass
                            
                            with waiting_players_lock:
                                player_stop_event = threading.Event()
                                if staying_player == player1_username:
                                    waiting_players.put((player1_adapter.conn, player1_addr, player1_username, player_stop_event))
                                    print(f"[INFO] Keeping {staying_player} in active_usernames and adding to waiting queue.")
                                else:
                                    waiting_players.put((player2_adapter.conn, player2_addr, player2_username, player_stop_event))
                                    print(f"[INFO] Keeping {staying_player} in active_usernames and adding to waiting queue.")
                        else:
                            send_packet(staying_conn, PACKET_TYPE_GAME_END, "No spectators available to play with you. Session ending.")
                            notify_spectators(f"{leaving_player} declined rematch and no spectators available. Game ending.")
                        
                        play_again = False
                    break

        if not play_again:
            print(f"[GAME SESSION {game_id}] Session ended.")
            try:
                if not game_ended_due_to_disconnect:
                    if not (player1_wants_rematch and not player2_wants_rematch):
                        send_packet(player1_adapter.conn, PACKET_TYPE_GAME_END, "Thank you for playing!")
                    if not (player2_wants_rematch and not player1_wants_rematch):
                        send_packet(player2_adapter.conn, PACKET_TYPE_GAME_END, "Thank you for playing!")
            except: pass
            notify_spectators("Game session has concluded.")

    except Exception as e:
        print(f"[FATAL ERROR in GAME SESSION {game_id}] Error: {e}\n{traceback.format_exc()}")
        try: send_packet(player1_adapter.conn, PACKET_TYPE_ERROR, "A fatal server error occurred. Game ending.")
        except: pass
        try: send_packet(player2_adapter.conn, PACKET_TYPE_ERROR, "A fatal server error occurred. Game ending.")
        except: pass
        notify_spectators(f"Game session ended due to a server error: {e}")
    finally:
        print(f"[GAME SESSION {game_id}] Cleaning up session.")
        
        staying_player_username = None
        if player1_wants_rematch and not player2_wants_rematch:
            staying_player_username = player1_username
        elif player2_wants_rematch and not player1_wants_rematch:
            staying_player_username = player2_username
        
        with active_usernames_lock:
            if player1_username in active_usernames and active_usernames.get(player1_username) == player1_adapter.conn:
                if staying_player_username != player1_username:
                    print(f"[DEBUG] Removing P1 ({player1_username}) of this session from active_usernames.")
                    del active_usernames[player1_username]
                else:
                    print(f"[DEBUG] Keeping P1 ({player1_username}) in active_usernames for next game.")
            elif player1_username in active_usernames:
                print(f"[DEBUG] P1 ({player1_username}) was in active_usernames but with a different connection. Not removing from active_usernames list by this ended session.")

            if player2_username in active_usernames and active_usernames.get(player2_username) == player2_adapter.conn:
                if staying_player_username != player2_username:
                    print(f"[DEBUG] Removing P2 ({player2_username}) of this session from active_usernames.")
                    del active_usernames[player2_username]
                else:
                    print(f"[DEBUG] Keeping P2 ({player2_username}) in active_usernames for next game.")
            elif player2_username in active_usernames:
                print(f"[DEBUG] P2 ({player2_username}) was in active_usernames but with a different connection. Not removing from active_usernames list by this ended session.")
        
        with disconnected_players_lock:
            if player1_username in disconnected_players and disconnected_players[player1_username].get('game_id') == game_id:
                del disconnected_players[player1_username]
            if player2_username in disconnected_players and disconnected_players[player2_username].get('game_id') == game_id:
                del disconnected_players[player2_username]
        
        if staying_player_username != player1_username:
            try: player1_adapter.conn.close()
            except: pass
        if staying_player_username != player2_username:
            try: player2_adapter.conn.close()
            except: pass
        
        with game_lock:
            game_in_progress = False
        current_game = DummyGame()
        print(f"[INFO] Game session {game_id} fully concluded. Server ready for new players or waiting players.")

def handle_reconnection(conn, addr, username):
    """Handles a player's reconnection attempt to an existing game session."""
    print(f"[RECONNECTION] Attempt for {username} from {addr}")
    with disconnected_players_lock:
        if username not in disconnected_players:
            print(f"[RECONNECTION] {username} not in disconnected_players list.")
            send_packet(conn, PACKET_TYPE_ERROR, "No prior disconnected game session found for your username.")
            return False
            
        player_data = disconnected_players[username]
        disconnect_time = player_data['disconnect_time']
        
        if time.time() - disconnect_time > RECONNECT_TIMEOUT:
            print(f"[RECONNECTION] {username} window expired. Removing from disconnected_players.")
            send_packet(conn, PACKET_TYPE_ERROR, f"Reconnection window expired.")
            del disconnected_players[username]
            return False

    with active_usernames_lock:
        if username in active_usernames:
            old_conn = active_usernames[username]
            print(f"[RECONNECTION] {username} was already in active_usernames. Closing old connection.")
            if old_conn and old_conn != conn:
                try:
                    send_packet(old_conn, PACKET_TYPE_ERROR, "Another client reconnected with your username. Closing this old session.")
                    old_conn.close()
                except Exception as e:
                    print(f"[RECONNECTION] Error closing old conn for {username}: {e}")
        active_usernames[username] = conn 
        print(f"[RECONNECTION] {username} from {addr} updated in active_usernames with new connection.")

    return True

def run_game_server():
    """Main server loop that handles connections and manages game sessions."""
    global waiting_players, waiting_players_lock, game_in_progress, current_game
    
    print(f"[INFO] Server listening on {HOST}:{PORT}")
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((HOST, PORT))
        server_socket.listen(5)
        
        while True:
            try:
                conn, addr = server_socket.accept()
                print(f"[INFO] New connection from {addr}")
                
                valid, header, payload = receive_packet(conn, timeout=5)
                
                if not valid or not payload:
                    print(f"[WARNING] Connection from {addr} failed to send valid packet. Closing.")
                    conn.close()
                    continue
                    
                magic, seq, packet_type, data_len = header
                payload_str = payload.decode() if isinstance(payload, bytes) else payload
                
                if packet_type != PACKET_TYPE_USERNAME:
                    print(f"[WARNING] Connection from {addr} did not send a valid USERNAME packet first. Closing.")
                    send_packet(conn, PACKET_TYPE_ERROR, "Expected USERNAME packet first. Closing connection.")
                    conn.close()
                    continue
                
                username = payload_str
                if not username:
                    print(f"[WARNING] Connection from {addr} sent an empty username. Closing.")
                    send_packet(conn, PACKET_TYPE_ERROR, "Username cannot be empty. Closing connection.")
                    conn.close()
                    continue
                print(f"[INFO] Received username: {username} from {addr}")
                
                available, message = check_username_available(username)
                
                if not available:
                    if message == "disconnected":
                        print(f"[INFO] {username} is attempting to reconnect.")
                        if handle_reconnection(conn, addr, username):
                            print(f"[DEBUG] Reconnection handled for {username}. Main server loop continuing to next accept.")
                            continue 
                        else:
                            try:
                                send_packet(conn, PACKET_TYPE_ERROR, "Failed to process reconnection. Please try a new connection.")
                                time.sleep(0.1)
                            except Exception as e_send_err:
                                print(f"[WARNING] Failed to send reconnection failure message to {addr}: {e_send_err}")
                            finally:
                                conn.close()
                            continue
                    else:
                        print(f"[WARNING] Username {username} is already in use or another issue: {message}. Closing connection.")
                        try:
                            send_packet(conn, PACKET_TYPE_ERROR, message)
                            time.sleep(0.1)
                        except Exception as e_send_err:
                            print(f"[WARNING] Failed to send error message '{message}' to {addr}: {e_send_err}")
                        finally:
                            conn.close()
                        continue
                
                with active_usernames_lock:
                    active_usernames[username] = conn

                print(f"[DEBUG] {username} processed. Checking game_in_progress / waiting queue.")
                with game_lock:
                    if game_in_progress:
                        print(f"[INFO] Game in progress. {username}@{addr} will be a spectator.")
                        threading.Thread(target=handle_spectator,
                                      args=(conn, addr, current_game), 
                                      daemon=True).start()
                    else:
                        with waiting_players_lock:
                            if waiting_players.qsize() >= 1:
                                player1_conn, player1_addr, player1_username, player1_stop_event = waiting_players.get()
                                print(f"[INFO] Signalling waiting player {player1_username} to stop their waiting thread.")
                                player1_stop_event.set()
                                time.sleep(0.3)

                                print(f"[INFO] Found waiting player: {player1_username}@{player1_addr}. Starting game with {username}@{addr}.")
                                game_in_progress = True
                                
                                game_id_for_session = f"{player1_username}_vs_{username}_{int(time.time())}"
                                current_game = RealGame(player1_username, username, game_id_for_session)
                                
                                player_info_update_msg = f"SPECTATOR_PLAYER_NAMES:P1={player1_username},P2={username}"
                                with spectators_lock:
                                    for spec_conn in current_game_spectators:
                                        if spec_conn != player1_conn and spec_conn != conn:
                                            _send_spectator_message(spec_conn, PACKET_TYPE_CHAT, player_info_update_msg, "update player names for new game")

                                threading.Thread(target=handle_game_session, 
                                              args=(player1_conn, conn, player1_addr, addr, player1_username, username, game_id_for_session),
                                              daemon=True).start()
                            else:
                                print(f"[INFO] No game in progress and no waiting players. Adding {username}@{addr} to waiting queue.")
                                player_stop_event = threading.Event()
                                waiting_players.put((conn, addr, username, player_stop_event))
                                threading.Thread(target=handle_waiting_player,
                                              args=(conn, addr, username, player_stop_event),
                                              daemon=True).start()
                
            except KeyboardInterrupt:
                print("[INFO] Server shutting down by keyboard interrupt")
                break
            except Exception as e:
                print(f"[ERROR] Unexpected server error: {e}")
                if 'username' in locals() and username:
                    with active_usernames_lock:
                        if username in active_usernames and active_usernames.get(username) == conn :
                            print(f"[ERROR_CLEANUP] Removing {username} from active_usernames due to server loop error.")
                            del active_usernames[username]
                    try:
                        if conn: conn.close()
                    except: pass
                continue

            with game_lock:
                if not game_in_progress:
                    with waiting_players_lock:
                        if waiting_players.empty():
                            with spectators_lock:
                                for spec_conn in list(current_game_spectators):
                                    if waiting_players.qsize() >= 2:
                                        break
                                    try:
                                        if send_packet(spec_conn, PACKET_TYPE_CHAT, "The previous game has ended. Would you like to play in the next game? (Type YES within 10s to join queue):"):
                                            is_valid_resp, header_resp, payload_resp = receive_packet(spec_conn, timeout=10.0)
                                            if is_valid_resp and header_resp and payload_resp:
                                                resp_str = payload_resp.decode().strip().upper()
                                                if resp_str == "YES":
                                                    send_packet(spec_conn, PACKET_TYPE_CHAT, "Please reconnect with a username to join the game queue.")
                                                    current_game_spectators.remove(spec_conn)
                                                    try:
                                                        spec_conn.close()
                                                    except: pass
                                                else:
                                                    send_packet(spec_conn, PACKET_TYPE_CHAT, "Okay, you will remain a spectator if a new game starts.")
                                        else:
                                            current_game_spectators.remove(spec_conn)
                                            try: spec_conn.close()
                                            except: pass
                                    except Exception as e_spec_poll:
                                        print(f"[INFO] Error while polling spectator {spec_conn.getpeername() if hasattr(spec_conn, 'getpeername') else 'unknown_spec'} to play: {e_spec_poll}. Removing.")
                                        if spec_conn in current_game_spectators:
                                            current_game_spectators.remove(spec_conn)
                                        try: spec_conn.close()
                                        except: pass

def main():
    """Entry point for the server application."""
    try:
        run_game_server()
    except KeyboardInterrupt:
        print("\n[INFO] Server shutdown requested. Exiting...")
    except Exception as e:
        print(f"[ERROR] Fatal server error: {e}")
        traceback.print_exc()

def check_username_available(username):
    """Checks if a username is available for a new connection or eligible for reconnection."""
    with active_usernames_lock:
        print(f"[DEBUG]   active_usernames: {list(active_usernames.keys())}")
    with disconnected_players_lock:
        print(f"[DEBUG]   disconnected_players: {list(disconnected_players.keys())}")

    with disconnected_players_lock:
        if username in disconnected_players:
            elapsed = time.time() - disconnected_players[username]['disconnect_time']
            if elapsed <= RECONNECT_TIMEOUT:
                print(f"[DEBUG] '{username}' reconnect within {elapsed:.1f}s → treating as disconnected.")
                return (False, "disconnected")
            else:
                print(f"[DEBUG] '{username}' reconnection window expired ({elapsed:.1f}s).  Dropping record.")
                del disconnected_players[username]

    with active_usernames_lock:
        if username in active_usernames:
            print(f"[DEBUG] '{username}' is still active -> rejecting as in use.")
            return (False, "Username already in use by another player.")

    print(f"[DEBUG] '{username}' is available for a new session.")
    return (True, None)

def handle_ship_placement(player_rfile, player_wfile, player_board_obj, p_name, opponent_board_obj=None):
    """Handles the ship placement phase of a game session."""
    send_to_player(player_wfile, f"{p_name}, it's time to place your ships!")
    send_to_player(player_wfile, "Would you like to place ships manually (M) or randomly (R)? [M/R]:")
    
    choice = None
    try:
        choice = recv_from_player_with_timeout(player_rfile, MOVE_TIMEOUT, p_name)
    except PlayerDisconnectedError:
        raise

    if choice is None:
        send_to_player(player_wfile, "No selection made within timeout period. Ships will be placed randomly.")
        player_board_obj.place_ships_randomly(SHIPS)
        send_to_player(player_wfile, "Ships have been placed randomly on your board.")
        send_board_to_player(player_wfile, player_board_obj, opponent_board_obj)
        return True

    choice = choice.upper()[0] if choice else ""
    
    if choice == 'M':
        for ship_name, ship_size in SHIPS:
            placed = False
            while not placed:
                send_board_to_player(player_wfile, player_board_obj, opponent_board_obj) 
                send_to_player(player_wfile, f"Placing your {ship_name} (size {ship_size}).")
                send_to_player(player_wfile, "Enter starting coordinate and orientation (e.g. A1 H or B2 V):")
                
                combined_input = None
                try:
                    combined_input = recv_from_player_with_timeout(player_rfile, MOVE_TIMEOUT, p_name)
                except PlayerDisconnectedError:
                    raise

                if combined_input is None:
                    send_to_player(player_wfile, f"Timeout waiting for input. {ship_name} will be placed randomly.")
                    randomly_place_single_ship(player_board_obj, ship_name, ship_size)
                    send_to_player(player_wfile, f"{ship_name} placed randomly.")
                    placed = True
                    continue
                elif combined_input.lower() == 'quit':
                    raise PlayerDisconnectedError(p_name, None)

                try:
                    parts = combined_input.strip().upper().split()
                    if len(parts) != 2:
                        send_to_player(player_wfile, "Invalid format. Expected coordinate and orientation (e.g., A1 H).")
                        continue
                    
                    coord_str, orientation_char = parts[0], parts[1]
                    row, col = parse_coordinate(coord_str)
                    orientation_enum = 0 if orientation_char == 'H' else (1 if orientation_char == 'V' else -1)

                    if orientation_enum == -1:
                        send_to_player(player_wfile, "Invalid orientation. Please enter 'H' or 'V'.")
                        continue
                    
                    if player_board_obj.can_place_ship(row, col, ship_size, orientation_enum):
                        occupied_positions = player_board_obj.do_place_ship(row, col, ship_size, orientation_enum)
                        player_board_obj.placed_ships.append({'name': ship_name, 'positions': occupied_positions})
                        send_to_player(player_wfile, f"{ship_name} placed successfully!")
                        placed = True
                    else:
                        send_to_player(player_wfile, f"Cannot place {ship_name} at {coord_str} (orientation={orientation_char}). Try again.")
                except ValueError as e:
                    send_to_player(player_wfile, f"Invalid input: {e}. Try again.")
            
        send_to_player(player_wfile, "All ships placed successfully!")
        send_board_to_player(player_wfile, player_board_obj, opponent_board_obj)
        return True
    else:
        player_board_obj.place_ships_randomly(SHIPS)
        send_to_player(player_wfile, "Ships have been placed randomly on your board.")
        send_board_to_player(player_wfile, player_board_obj, opponent_board_obj)
        return True

if __name__ == "__main__":
    main()