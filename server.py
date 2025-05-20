"""
server.py

Serves Battleship game sessions to connected clients (clients can change over time).
Game logic is handled entirely on the server using battleship.py.
Client sends FIRE commands, and receives game feedback.
Supports multiple games in sequence without restarting the server, both with the same players
or with entirely new connections.
Uses a custom packet protocol.
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

HOST = '127.0.0.1'
PORT = 5001
CONNECTION_TIMEOUT = 60  # seconds to wait for a connection
HEARTBEAT_INTERVAL = 30  # seconds between heartbeat checks
MOVE_TIMEOUT = 30  # seconds a player has to make a move
RECONNECT_TIMEOUT = 60  # seconds a player can reconnect after disconnection

# Global variables for game state
game_in_progress = False
game_lock = threading.Lock()
waiting_players = queue.Queue()
waiting_players_lock = threading.Lock()
current_game_spectators = []  # List of spectator connections
spectators_lock = threading.Lock()

# Track usernames and disconnected players for reconnection
active_usernames = {}  # username -> connection
active_usernames_lock = threading.Lock()
disconnected_players = {}  # username -> {opponent, board, disconnect_time}
disconnected_players_lock = threading.Lock()

# Player tracking for reconnections
player_connections = {}  # username -> connection socket
player_connections_lock = threading.Lock()
current_games = {}  # game_id -> {player1_username, player2_username, started_time}
current_games_lock = threading.Lock()

# Global game object for spectators
from battleship import BOARD_SIZE
class DummyGame: # This is a dummy game object for spectators when no real game is in progress - earlier issues with this
    def __init__(self):
        self.board_size = BOARD_SIZE
        self.player1 = "Waiting for players"
        self.player2 = "Waiting for players"
        self.current_turn = None
        self.game_state = "waiting"
        self.last_move = None
        self.last_move_result = None

class RealGame:
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

# Global chat system
def broadcast_chat_message(sender_username, message):
    """
    Broadcast a chat message to all connected players and spectators.
    
    Args:
        sender_username: The username of the message sender
        message: The chat message text
    """
    chat_msg = f"[CHAT] {sender_username}: {message}"
    print(f"[INFO] Broadcasting chat: {chat_msg}")
    
    # Get all active connections to send to
    recipients = []
    
    # Add active players
    with active_usernames_lock:
        for username, conn in active_usernames.items():
            recipients.append((username, conn))
    
    # Add spectators
    with spectators_lock:
        for conn in current_game_spectators:
            # Spectators don't have usernames in this list, add with None
            recipients.append((None, conn))
    
    # Send to all recipients
    for username, conn in recipients:
        try:
            # Don't echo message back to sender
            if username == sender_username:
                continue
                
            send_packet(conn, PACKET_TYPE_CHAT, chat_msg)
        except:
            pass

class ProtocolAdapter:
    def __init__(self, conn, username):
        self.conn = conn
        self.username = username
        self.buffer = []
        self.last_packet_type = None
        self.grid_mode = False
        
    def readline(self):
        """Read a line from the buffer or wait for a new packet"""
        if self.buffer:
            return self.buffer.pop(0)
            
        valid, header, payload = receive_packet(self.conn, timeout=MOVE_TIMEOUT)
        if not valid or not payload:
            raise ConnectionResetError("Failed to receive packet")
            
        payload_str = payload.decode() if isinstance(payload, bytes) else payload
        magic, seq, packet_type, data_len = header
        
        # Save the last packet type
        self.last_packet_type = packet_type
        
        # Handle different packet types
        if packet_type == PACKET_TYPE_MOVE:
            return payload_str + "\n"
        elif packet_type == PACKET_TYPE_CHAT:
            # Process chat message
            # If it's a game-relevant input like M/R for ship placement, handle as command
            # Otherwise, broadcast as chat message
            if payload_str.upper() in ['M', 'R', 'H', 'V', 'Y', 'N', 'YES', 'NO']:
                return payload_str + "\n"
            else:
                # Broadcast chat message from this player
                broadcast_chat_message(self.username, payload_str)
                return "\n"  # Return empty line to not affect game flow
        elif packet_type == PACKET_TYPE_DISCONNECT:
            raise ConnectionResetError("Player disconnected")
        else:
            # Return empty string for other packet types
            return "\n"
            
    def write(self, msg):
        """Write a message to be sent as a packet. Raises PlayerDisconnectedError on send failure."""
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
                print(f"[ADAPTER ERROR] send_packet failed for CHAT in write() for {player_name_for_error}")
                raise PlayerDisconnectedError(player_name_for_error, None)
        return len(msg)
        
    def flush(self):
        """
        Send any buffered grid updates. 
        Returns True on success. Raises PlayerDisconnectedError on failure.
        """
        player_name_for_error = self.username if self.username else "UnknownPlayerAdapterUser"
        if self.buffer:
            grid_msg_to_send = ''.join(self.buffer)
            self.buffer = [] 
            
            self.grid_mode = False 

            if not send_packet(self.conn, PACKET_TYPE_BOARD_UPDATE, grid_msg_to_send):
                print(f"[ADAPTER ERROR] send_packet failed for BOARD_UPDATE in flush() for {player_name_for_error}")
                raise PlayerDisconnectedError(player_name_for_error, None)
        return True

def handle_player_disconnect(player_conn, player_name):
    """
    Handle a player disconnection during gameplay.
    Marks the player as disconnected and starts the reconnection window.
    """
    print(f"[DEBUG] Handling disconnection for player {player_name}")
    
    with disconnected_players_lock:
        disconnected_players[player_name] = {
            'disconnect_time': time.time(),
        }
        print(f"[INFO] {player_name} marked as disconnected. Reconnection window: {RECONNECT_TIMEOUT} seconds")
    
    # Clean up active usernames
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
    """
    Ask a player if they want to play again.
    Returns True if they want to play again, False otherwise.
    """
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
    """
    Handle a player in the waiting lobby.
    Sends waiting messages and manages the connection until a game slot is available
    or until signaled to stop by stop_event.
    """
    print(f"[INFO] {username} entered waiting lobby.")
    is_active_player = True

    try:
        send_packet(conn, PACKET_TYPE_CHAT, "\nYou are in the waiting lobby. Waiting for another player...")
        send_packet(conn, PACKET_TYPE_CHAT, "Type 'quit' to leave the waiting lobby, or send messages to chat with others.")
        
        last_status_update_time = time.time()

        while not stop_event.is_set():
            if stop_event.wait(timeout=0.2):
                print(f"[DEBUG] handle_waiting_player for {username} detected stop_event. Exiting loop.")
                is_active_player = False
                break

            try:
                valid, header, payload = receive_packet(conn, timeout=2.0) 
                
                if stop_event.is_set():
                    print(f"[DEBUG] handle_waiting_player for {username} detected stop_event after receive_packet. Exiting loop.")
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
                elif payload is None and not valid and header is None :
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
    """Handle a spectator connection."""
    print(f"[DEBUG] New spectator connection from {addr}")
    spectator_username = f"Spectator@{addr[0]}:{addr[1]}"
    
    try:
        if not send_packet(conn, PACKET_TYPE_CHAT, "\nWelcome! You are now spectating a Battleship game."):
            print("[DEBUG] Failed to send welcome message")
            return
            
        if not send_packet(conn, PACKET_TYPE_CHAT, "You will see all game updates but cannot participate in the game."):
            print("[DEBUG] Failed to send welcome message")
            return
            
        if not send_packet(conn, PACKET_TYPE_CHAT, "Type 'quit' to stop spectating. You can send chat messages that will be seen by all players and spectators."):
            print("[DEBUG] Failed to send welcome message")
            return
        
        # Send current game state information
        game_state_message = f"\nCurrent Game Status:\n"
        game_state_message += f"Player 1: {game.player1}\n"
        game_state_message += f"Player 2: {game.player2}\n"
        game_state_message += f"Game State: {game.game_state}\n"
        
        if game.current_turn:
            game_state_message += f"Current Turn: {game.current_turn}\n"
        else:
            game_state_message += "Waiting for game to start...\n"
            
        if not send_packet(conn, PACKET_TYPE_CHAT, game_state_message):
            print("[DEBUG] Failed to send game state message")
            return

        # Add spectator to the list
        with spectators_lock:
            current_game_spectators.append(conn)
            print(f"[DEBUG] Added spectator to list. Total spectators: {len(current_game_spectators)}")

        # Broadcast that a new spectator joined
        broadcast_chat_message("SERVER", f"A new spectator has joined to watch the game")

        # Set a longer timeout for spectators
        conn.settimeout(30)
        
        last_heartbeat = time.time()
        heartbeat_interval = 15
        
        last_status_update = time.time()
        status_update_interval = 10
        
        while True:
            try:
                current_time = time.time()
                
                if current_time - last_heartbeat >= heartbeat_interval:
                    print("[DEBUG] Sending spectator heartbeat")
                    if not send_packet(conn, PACKET_TYPE_HEARTBEAT, b''):
                        print("[DEBUG] Failed to send heartbeat")
                        break
                    last_heartbeat = current_time
                
                if current_time - last_status_update >= status_update_interval:
                    status_message = f"\nGame Status Update:\n"
                    status_message += f"Game State: {game.game_state}\n"
                    if game.current_turn:
                        status_message += f"Current Turn: {game.current_turn}\n"
                    if game.last_move:
                        status_message += f"Last Move: {game.last_move}\n"
                    if game.last_move_result:
                        status_message += f"Result: {game.last_move_result}\n"
                        
                    if not send_packet(conn, PACKET_TYPE_CHAT, status_message):
                        print("[DEBUG] Failed to send status update")
                        break
                    last_status_update = current_time
                
                is_valid, header, payload = receive_packet(conn, timeout=1.0)
                if not is_valid and header is not None:
                    print("[DEBUG] Received invalid packet from spectator")
                    continue
                    
                if header is None:
                    continue
                    
                magic, seq, ptype, dlen = header
                print(f"[DEBUG] Received packet from spectator: type={get_packet_type_name(ptype)}")
                
                if ptype == PACKET_TYPE_HEARTBEAT:
                    print("[DEBUG] Received heartbeat from spectator")
                    if not send_packet(conn, PACKET_TYPE_ACK, b''):
                        print("[DEBUG] Failed to send heartbeat ACK")
                        break
                elif ptype == PACKET_TYPE_ACK:
                    print("[DEBUG] Received ACK from spectator")
                    continue
                elif ptype == PACKET_TYPE_CHAT:
                    payload_str = payload.decode() if isinstance(payload, bytes) else payload
                    if payload_str.lower() == 'quit':
                        print(f"[DEBUG] Spectator {addr} requested to quit")
                        if not send_packet(conn, PACKET_TYPE_CHAT, "You have left the spectator mode. Goodbye!"):
                            print("[DEBUG] Failed to send goodbye message")
                        break
                    else:
                        broadcast_chat_message(spectator_username, payload_str)
                elif ptype == PACKET_TYPE_MOVE:
                    payload_str = payload.decode() if isinstance(payload, bytes) else payload
                    if not send_packet(conn, PACKET_TYPE_CHAT, f"As a spectator, you cannot make moves. Type 'quit' to leave or send chat messages."):
                        print("[DEBUG] Failed to send spectator restriction message")
                else:
                    print(f"[DEBUG] Unexpected packet type from spectator: {get_packet_type_name(ptype)}")
                    if not send_packet(conn, PACKET_TYPE_CHAT, "As a spectator, you can use 'quit' to leave or send chat messages."):
                        print("[DEBUG] Failed to send help message")
                    continue
                    
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[DEBUG] Error handling spectator: {e}")
                break
                
    except Exception as e:
        print(f"[DEBUG] Fatal error in spectator handler: {e}")
    finally:
        print(f"[DEBUG] Closing spectator connection from {addr}")
        # Remove from spectators list
        with spectators_lock:
            if conn in current_game_spectators:
                current_game_spectators.remove(conn)
                print(f"[DEBUG] Removed spectator from list. Remaining spectators: {len(current_game_spectators)}")
                
        # Notify others that the spectator left
        broadcast_chat_message("SERVER", f"A spectator has left the game")
        conn.close()

def notify_spectators(message):
    """
    Send a message to all spectators.
    """
    with spectators_lock:
        for conn in current_game_spectators[:]: 
            try:
                send_packet(conn, PACKET_TYPE_BOARD_UPDATE, message)
            except:
                # Remove disconnected spectator
                current_game_spectators.remove(conn)

def handle_game_session(player1_conn, player2_conn, player1_addr, player2_addr, player1_username, player2_username, game_id):
    """
    Handle a game session between two connected players.
    Manages multiple games in succession if players choose to play again.
    When this function returns, the connections will be closed.
    """
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
                    player1_username=player1_username, player2_username=player2_username,
                    initial_player1_board_state=current_p1_board_state,
                    initial_player2_board_state=current_p2_board_state,
                    initial_current_player_name=next_player_for_turn
                )
                current_game.game_state = "completed"

            except PlayerDisconnectedError as pde:
                game_ended_due_to_disconnect = True
                print(f"[GAME SESSION {game_id}] PlayerDisconnectedError: {pde.player_name} disconnected.")
                
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

                # Notify the other player and spectators
                msg_for_other = f"\n{disconnected_player_name} has disconnected. Waiting {RECONNECT_TIMEOUT} seconds for reconnection..."
                msg_for_spectators = f"{disconnected_player_name} has disconnected. Waiting for reconnection..."
                try:
                    send_packet(other_player_adapter.conn, PACKET_TYPE_CHAT, msg_for_other)
                except Exception as e:
                    print(f"[GAME SESSION {game_id}] Error notifying {other_player_name} of disconnect: {e}")
                notify_spectators(msg_for_spectators)
                current_game.game_state = "interrupted_waiting_reconnect"

                # Reconnection wait loop
                reconnected_successfully = False
                wait_start_time = time.time()
                while time.time() - wait_start_time < RECONNECT_TIMEOUT:
                    with active_usernames_lock:
                        if disconnected_player_name in active_usernames:
                            print(f"[GAME SESSION {game_id}] {disconnected_player_name} appears in active_usernames. Attempting to resume.")
                            
                            # Retrieve their saved state for this game
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
                    
                    # Notify waiting player occasionally
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

                else: # Did not reconnect in time
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
                    play_again = False
                    if not player1_wants_rematch:
                        send_packet(player1_adapter.conn, PACKET_TYPE_GAME_END, "You declined rematch. Session ending.")
                        send_packet(player2_adapter.conn, PACKET_TYPE_GAME_END, f"{player1_username} declined rematch. Session ending.")
                        notify_spectators(f"{player1_username} declined a rematch.")
                    elif not player2_wants_rematch:
                        send_packet(player2_adapter.conn, PACKET_TYPE_GAME_END, "You declined rematch. Session ending.")
                        send_packet(player1_adapter.conn, PACKET_TYPE_GAME_END, f"{player2_username} declined rematch. Session ending.")
                        notify_spectators(f"{player2_username} declined a rematch.")
                    break

        if not play_again:
            print(f"[GAME SESSION {game_id}] Session ended.")
            try:
                if not game_ended_due_to_disconnect:
                    send_packet(player1_adapter.conn, PACKET_TYPE_GAME_END, "Thank you for playing!")
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
        with active_usernames_lock:
            if player1_username in active_usernames and active_usernames.get(player1_username) == player1_adapter.conn:
                print(f"[DEBUG] Removing P1 ({player1_username}) of this session from active_usernames.")
                del active_usernames[player1_username]
            elif player1_username in active_usernames:
                 print(f"[DEBUG] P1 ({player1_username}) was in active_usernames but with a different connection. Not removing from active_usernames list by this ended session.")

            if player2_username in active_usernames and active_usernames.get(player2_username) == player2_adapter.conn:
                print(f"[DEBUG] Removing P2 ({player2_username}) of this session from active_usernames.")
                del active_usernames[player2_username]
            elif player2_username in active_usernames:
                print(f"[DEBUG] P2 ({player2_username}) was in active_usernames but with a different connection. Not removing from active_usernames list by this ended session.")
        
        with disconnected_players_lock:
            if player1_username in disconnected_players and disconnected_players[player1_username].get('game_id') == game_id:
                del disconnected_players[player1_username]
            if player2_username in disconnected_players and disconnected_players[player2_username].get('game_id') == game_id:
                del disconnected_players[player2_username]
        
        try: player1_adapter.conn.close()
        except: pass
        try: player2_adapter.conn.close()
        except: pass
        
        with game_lock:
            game_in_progress = False
        current_game = DummyGame()
        print(f"[INFO] Game session {game_id} fully concluded. Server ready for new players or waiting players.")

def handle_reconnection(conn, addr, username):
    """
    Handle a player reconnection attempt. Validates if eligible, updates active_usernames.
    Returns True if basic reconnection checks pass (eligible and active_usernames updated), False otherwise.
    Actual game state restoration happens in handle_game_session.
    """
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

    # If eligible, update active_usernames with the new connection
    with active_usernames_lock:
        if username in active_usernames:
            print(f"[RECONNECTION] {username} was already in active_usernames. Closing old connection.")
            try:
                old_conn = active_usernames[username]
                if old_conn != conn:
                    send_packet(old_conn, PACKET_TYPE_ERROR, "Another client reconnected with your username. Closing this old session.")
                    old_conn.close()
            except Exception as e:
                print(f"[RECONNECTION] Error closing old conn for {username}: {e}")
        active_usernames[username] = conn 
        print(f"[RECONNECTION] {username} from {addr} updated in active_usernames with new connection.")

    return True

def run_game_server():
    """
    Main server loop that handles connections and starts games.
    """
    global waiting_players, waiting_players_lock, game_in_progress, current_game
    
    print(f"[INFO] Server listening on {HOST}:{PORT}")
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((HOST, PORT))
        server_socket.listen(5)
        
        while True:
            try:
                # Accept new connections
                conn, addr = server_socket.accept()
                print(f"[INFO] New connection from {addr}")
                
                # Use protocol for username verification
                valid, header, payload = receive_packet(conn, timeout=5)
                
                if not valid or not payload:
                    print(f"[WARNING] Connection from {addr} failed to send valid packet. Closing.")
                    conn.close()
                    continue
                    
                magic, seq, packet_type, data_len = header
                payload_str = payload.decode() if isinstance(payload, bytes) else payload
                
                # Verify it's a username packet
                if packet_type != PACKET_TYPE_USERNAME:
                    print(f"[WARNING] Connection from {addr} did not send a valid USERNAME packet first. Closing.")
                    send_packet(conn, PACKET_TYPE_ERROR, "Expected USERNAME packet first. Closing connection.")
                    conn.close()
                    continue
                
                # Extract username
                username = payload_str
                if not username:
                    print(f"[WARNING] Connection from {addr} sent an empty username. Closing.")
                    send_packet(conn, PACKET_TYPE_ERROR, "Username cannot be empty. Closing connection.")
                    conn.close()
                    continue
                print(f"[INFO] Received username: {username} from {addr}")
                
                # Check if username is available or belongs to a disconnected player
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
                
                # Add username to active usernames
                with active_usernames_lock:
                    active_usernames[username] = conn

                print(f"[DEBUG] {username} processed. Checking game_in_progress / waiting queue.")
                with game_lock:
                    if game_in_progress:
                        # Game in progress, add as spectator
                        print(f"[INFO] Game in progress. {username}@{addr} will be a spectator.")
                        threading.Thread(target=handle_spectator,
                                      args=(conn, addr, current_game), 
                                      daemon=True).start()
                    else:
                        # No game in progress, check waiting queue
                        with waiting_players_lock:
                            if waiting_players.qsize() >= 1:
                                # Get waiting player
                                player1_conn, player1_addr, player1_username, player1_stop_event = waiting_players.get()
                                print(f"[INFO] Signalling waiting player {player1_username} to stop their waiting thread.")
                                player1_stop_event.set()
                                time.sleep(0.3)

                                print(f"[INFO] Found waiting player: {player1_username}@{player1_addr}. Starting game with {username}@{addr}.")
                                # Start game with two players
                                game_in_progress = True
                                
                                game_id_for_session = f"{player1_username}_vs_{username}_{int(time.time())}"
                                current_game = RealGame(player1_username, username, game_id_for_session)
                                
                                threading.Thread(target=handle_game_session, 
                                              args=(player1_conn, conn, player1_addr, addr, player1_username, username, game_id_for_session),
                                              daemon=True).start()
                            else:
                                # Add to waiting queue
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
    """
    Entry point for the server.
    """
    try:
        run_game_server()
    except KeyboardInterrupt:
        print("\n[INFO] Server shutdown requested. Exiting...")
    except Exception as e:
        print(f"[ERROR] Fatal server error: {e}")
        traceback.print_exc()

def check_username_available(username):
    """
    Check if a username is available or if it belongs to a disconnected player.
    Also cleans up stale entries from active_usernames if a heartbeat-ack fails.
    Returns:
    - (True, None) if username is available for a new session.
    - (False, "disconnected") if username belongs to a disconnected player eligible for reconnection with the NEW connection.
    - (False, "error message") if username is genuinely in use by another active player or other error.
    """
    print(f"[DEBUG] Checking username availability for '{username}'")
    
    with active_usernames_lock:
        if username in active_usernames:
            existing_conn = active_usernames.get(username)
            if not existing_conn:
                print(f"[DEBUG] Anomaly: '{username}' key was in active_usernames but value was None. Treating as available.")
            else:
                print(f"[DEBUG] Username '{username}' found in active_usernames. Verifying existing connection status with Heartbeat-ACK.")
                is_connection_truly_alive = False
                try:
                    if send_packet(existing_conn, PACKET_TYPE_HEARTBEAT, ""):
                        valid_ack, header_ack, payload_ack = receive_packet(existing_conn, timeout=2.0) 
                        if valid_ack and header_ack and header_ack[2] == PACKET_TYPE_ACK:
                            is_connection_truly_alive = True
                            print(f"[DEBUG] Heartbeat-ACK received from '{username}'. Connection is live.")
                        else:
                            print(f"[DEBUG] Heartbeat sent to '{username}', but no/invalid ACK received. (valid_ack={valid_ack}, header_type={get_packet_type_name(header_ack[2]) if header_ack else 'N/A'}). Treating as stale.")
                    else:
                        print(f"[DEBUG] Heartbeat send to '{username}' failed (send_packet returned False). Treating as stale.")
                except socket.timeout:
                    print(f"[DEBUG] Socket timeout waiting for ACK from '{username}'. Treating as stale.")
                except (socket.error, BrokenPipeError, ConnectionResetError) as e_sock:
                    print(f"[DEBUG] Socket error during Heartbeat-ACK with '{username}': {e_sock}. Treating as stale.")
                except Exception as e_other:
                    print(f"[DEBUG] Unexpected error during Heartbeat-ACK with '{username}': {e_other}. Treating as stale.")

                if is_connection_truly_alive:
                    return (False, "Username already in use by another player.")
                else:
                    print(f"[DEBUG] Cleaning up stale/dead active connection for '{username}'.")
                    try:
                        existing_conn.close()
                    except Exception as e_close:
                        print(f"[DEBUG] Error closing stale connection for '{username}': {e_close}")
                    
                    if active_usernames.get(username) == existing_conn:
                        del active_usernames[username]

                    print(f"[DEBUG CUA Provisional Check] For username '{username}'. game_in_progress: {game_in_progress}, current_game is RealGame: {isinstance(current_game, RealGame)}")
                    if game_in_progress and isinstance(current_game, RealGame):
                        username_lower = username.lower()
                        cg_player1_lower = current_game.player1.lower() if hasattr(current_game, 'player1') and current_game.player1 else ""
                        cg_player2_lower = current_game.player2.lower() if hasattr(current_game, 'player2') and current_game.player2 else ""
                        
                        print(f"[DEBUG CUA Provisional Check] Comparing '{username_lower}' with CG P1 '{cg_player1_lower}' and CG P2 '{cg_player2_lower}'")
                        
                        is_player_in_current_game = False
                        if cg_player1_lower and username_lower == cg_player1_lower:
                            is_player_in_current_game = True
                        elif cg_player2_lower and username_lower == cg_player2_lower:
                            is_player_in_current_game = True

                        if is_player_in_current_game:
                            game_id_of_active_game = current_game.game_id
                            opponent_name = current_game.player2 if username_lower == cg_player1_lower else current_game.player1
                            
                            with disconnected_players_lock:
                                needs_provisional_marking = True
                                if username in disconnected_players:
                                    player_entry = disconnected_players[username]
                                    if player_entry.get('game_id') == game_id_of_active_game and player_entry.get('game_state') is not None:
                                        needs_provisional_marking = False
                                
                                if needs_provisional_marking:
                                    print(f"[DEBUG] Provisionally marking original-case '{username}' as disconnected from game '{game_id_of_active_game}'.")
                                    disconnected_players[username] = {
                                        'disconnect_time': time.time(),
                                        'opponent_username': opponent_name,
                                        'game_id': game_id_of_active_game,
                                        'game_state': None,
                                        'source': 'provisional_from_check_username_available'
                                    }
                                else:
                                    print(f"[DEBUG] '{username}' already in disconnected_players for game '{game_id_of_active_game}' with game_state. No provisional update needed.")
                        else:
                            print(f"[DEBUG CUA Provisional Check] '{username}' not part of current game players ('{current_game.player1}', '{current_game.player2}'). No provisional marking.")
                    else:
                        print(f"[DEBUG CUA Provisional Check] Game not in progress or not RealGame instance. No provisional marking for '{username}'.")


    with disconnected_players_lock:
        if username in disconnected_players:
            player_data = disconnected_players[username]
            disconnect_time = player_data['disconnect_time']
            elapsed = time.time() - disconnect_time
            
            if elapsed <= RECONNECT_TIMEOUT:
                print(f"[DEBUG] '{username}' is in disconnected_players and within reconnection window ({elapsed:.1f}s). Eligible for reconnect.")
                return (False, "disconnected") 
            else:
                print(f"[DEBUG] '{username}' was in disconnected_players, but window expired ({elapsed:.1f}s). Removing.")
                del disconnected_players[username]

    print(f"[DEBUG] '{username}' is available for a new session.")
    return (True, None)

if __name__ == "__main__":
    main()