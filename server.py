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
from battleship import run_two_player_game
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
active_players = {}  # username -> {board, opponent_username, game_thread, last_active, disconnected_time}
active_players_lock = threading.Lock()
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

class RealGame: # This is a real game object for players when a game is in progress
    def __init__(self, player1, player2):
        self.board_size = BOARD_SIZE
        self.player1 = player1
        self.player2 = player2
        self.current_turn = player1
        self.game_state = "setup"
        self.last_move = None
        self.last_move_result = None

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
        """Write a message to be sent as a packet"""
        if msg.strip() == "Your Grid:" or msg.strip() == "Opponent's Grid:" or msg.strip() == "SPECTATOR_GRID":
            self.grid_mode = True
            self.buffer = [msg]
        elif self.grid_mode and (msg.strip() == "" or msg == "\n"):
            # Empty line after grid - exit grid mode and send the accumulated grid
            self.grid_mode = False
            self.flush()
        elif self.grid_mode:
            # In grid mode, accumulate lines
            self.buffer.append(msg)
        else:
            # Regular message
            send_packet(self.conn, PACKET_TYPE_CHAT, msg.strip())
        return len(msg)
        
    def flush(self):
        """Send any buffered grid updates"""
        if self.buffer:
            grid_msg = ''.join(self.buffer)
            send_packet(self.conn, PACKET_TYPE_BOARD_UPDATE, grid_msg)
            self.buffer = []

def handle_player_disconnect(player_conn, player_name):
    """
    Handle a player disconnection during gameplay.
    Marks the player as disconnected and starts the reconnection window.
    """
    print(f"[DEBUG] Handling disconnection for player {player_name}")
    
    # First add to disconnected players to preserve reconnection data
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

def handle_waiting_player(conn, addr, username):
    """
    Handle a player in the waiting lobby.
    Sends waiting messages and manages the connection until a game slot is available.
    """
    global waiting_players, waiting_players_lock, game_in_progress
    
    print(f"[INFO] {username} entered waiting lobby.")
    
    try:
        # Add player to waiting queue
        with waiting_players_lock:
            waiting_players.put((conn, addr, username))
            position = waiting_players.qsize()
            
        # Send initial waiting message
        send_packet(conn, PACKET_TYPE_CHAT, f"\nYou are in the waiting lobby. Position: {position}")
        send_packet(conn, PACKET_TYPE_CHAT, "You will be matched with another player when the current game ends.")
        send_packet(conn, PACKET_TYPE_CHAT, "Type 'quit' to leave the waiting lobby, or send messages to chat with others.")
        
        # Keep connection alive while waiting
        while True:
            try:
                # Check for player input using protocol with shorter timeout
                valid, header, payload = receive_packet(conn, timeout=3)
                
                if valid and payload:
                    payload_str = payload.decode() if isinstance(payload, bytes) else payload
                    magic, seq, packet_type, data_len = header
                    
                    if packet_type == PACKET_TYPE_CHAT:
                        if payload_str.lower() == 'quit':
                            print(f"[INFO] {username} has chosen to quit the waiting lobby.")
                            with waiting_players_lock:
                                # Remove player from queue if they're still in
                                temp_queue = queue.Queue()
                                while not waiting_players.empty():
                                    player = waiting_players.get()
                                    if player[0] != conn:  # Skip the quitting player
                                        temp_queue.put(player)
                                waiting_players = temp_queue
                            send_packet(conn, PACKET_TYPE_CHAT, "You have left the waiting lobby.")
                            
                            # Remove from active usernames
                            with active_usernames_lock:
                                if username in active_usernames:
                                    del active_usernames[username]
                                    print(f"[INFO] Removed {username} from active usernames after quitting waiting lobby.")
                                    
                            return
                        else:
                            # Broadcast chat message from waiting player
                            broadcast_chat_message(username, payload_str)
                    
                    elif packet_type == PACKET_TYPE_DISCONNECT:
                        # Handle player disconnect
                        print(f"[INFO] {username} has disconnected from the waiting lobby.")
                        with waiting_players_lock:
                            # Remove player from queue
                            temp_queue = queue.Queue()
                            while not waiting_players.empty():
                                player = waiting_players.get()
                                if player[0] != conn:
                                    temp_queue.put(player)
                            waiting_players = temp_queue
                            
                        # Remove from active usernames
                        with active_usernames_lock:
                            if username in active_usernames:
                                del active_usernames[username]
                                print(f"[INFO] Removed {username} from active usernames after disconnection from waiting lobby.")
                                
                        return
                
                # No need to update position on every loop, just occasionally
                if random.random() < 0.1:  # Update roughly 10% of the time to reduce processing
                    # Update position in queue
                    with waiting_players_lock:
                        # Create a temporary list to find position
                        temp_list = []
                        while not waiting_players.empty():
                            temp_list.append(waiting_players.get())
                        
                        # Find position in the list
                        position = 0
                        for i, player in enumerate(temp_list, 1):
                            if player[0] == conn:
                                position = i
                            waiting_players.put(player)  # Put all players back in queue
                        
                        if position == 0:  # Player is no longer in queue
                            print(f"[INFO] {username} is no longer in waiting queue.")
                            return
                    
                # Only send position update occasionally to reduce traffic
                if random.random() < 0.2:
                    send_packet(conn, PACKET_TYPE_CHAT, f"Your position in queue: {position}")
                
                # Send heartbeat occasionally to keep connection alive
                if random.random() < 0.1:
                    send_packet(conn, PACKET_TYPE_HEARTBEAT, "")
                
                # Short sleep to prevent CPU hogging
                time.sleep(0.5)
                
            except (ConnectionResetError, BrokenPipeError):
                print(f"[INFO] {username} disconnected from waiting lobby at {addr}")
                with waiting_players_lock:
                    # Remove disconnected player from queue
                    temp_queue = queue.Queue()
                    while not waiting_players.empty():
                        player = waiting_players.get()
                        if player[0] != conn:
                            temp_queue.put(player)
                    waiting_players = temp_queue
                
                # Remove from active usernames
                with active_usernames_lock:
                    if username in active_usernames:
                        del active_usernames[username]
                        print(f"[INFO] Removed {username} from active usernames after connection error in waiting lobby.")
                        
                return
            except Exception as e:
                print(f"[ERROR] Error handling waiting player {username}: {e}")
                
                # Clean up active username
                with active_usernames_lock:
                    if username in active_usernames:
                        del active_usernames[username]
                        print(f"[INFO] Removed {username} from active usernames due to error in waiting lobby.")
                        
                return
                
    except Exception as e:
        print(f"[ERROR] Error setting up waiting player {username}: {e}")
        
        # Remove from active usernames
        with active_usernames_lock:
            if username in active_usernames:
                del active_usernames[username]
                print(f"[INFO] Removed {username} from active usernames due to setup error in waiting lobby.")
                
        try:
            conn.close()
        except:
            pass

def handle_spectator(conn, addr, game):
    """Handle a spectator connection."""
    print(f"[DEBUG] New spectator connection from {addr}")
    spectator_username = f"Spectator@{addr[0]}:{addr[1]}"  # Create a name for the spectator
    
    try:
        # Send welcome messages first
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
        conn.settimeout(30)  # 30 second timeout
        
        last_heartbeat = time.time()
        heartbeat_interval = 15  # Send heartbeat every 15 seconds
        
        # Send a status update every few seconds
        last_status_update = time.time()
        status_update_interval = 10  # Update every 10 seconds
        
        while True:
            try:
                current_time = time.time()
                
                # Send heartbeat if needed
                if current_time - last_heartbeat >= heartbeat_interval:
                    print("[DEBUG] Sending spectator heartbeat")
                    if not send_packet(conn, PACKET_TYPE_HEARTBEAT, b''):
                        print("[DEBUG] Failed to send heartbeat")
                        break
                    last_heartbeat = current_time
                
                # Send status update if needed
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
                
                # Receive any data from spectator
                is_valid, header, payload = receive_packet(conn, timeout=1.0)
                if not is_valid and header is not None:
                    print("[DEBUG] Received invalid packet from spectator")
                    continue
                    
                if header is None:  # No data received
                    continue
                    
                magic, seq, ptype, dlen = header
                print(f"[DEBUG] Received packet from spectator: type={get_packet_type_name(ptype)}")
                
                if ptype == PACKET_TYPE_HEARTBEAT:
                    print("[DEBUG] Received heartbeat from spectator")
                    # Send ACK for heartbeat
                    if not send_packet(conn, PACKET_TYPE_ACK, b''):
                        print("[DEBUG] Failed to send heartbeat ACK")
                        break
                elif ptype == PACKET_TYPE_ACK:
                    print("[DEBUG] Received ACK from spectator")
                    # Acknowledge receipt
                    continue
                elif ptype == PACKET_TYPE_CHAT:
                    # Handle chat messages (e.g., 'quit')
                    payload_str = payload.decode() if isinstance(payload, bytes) else payload
                    if payload_str.lower() == 'quit':
                        print(f"[DEBUG] Spectator {addr} requested to quit")
                        if not send_packet(conn, PACKET_TYPE_CHAT, "You have left the spectator mode. Goodbye!"):
                            print("[DEBUG] Failed to send goodbye message")
                        break
                    else:
                        # Broadcast the chat message to all players and spectators
                        broadcast_chat_message(spectator_username, payload_str)
                elif ptype == PACKET_TYPE_MOVE:
                    # Explain that spectators can't make moves
                    payload_str = payload.decode() if isinstance(payload, bytes) else payload
                    if not send_packet(conn, PACKET_TYPE_CHAT, f"As a spectator, you cannot make moves. Type 'quit' to leave or send chat messages."):
                        print("[DEBUG] Failed to send spectator restriction message")
                else:
                    print(f"[DEBUG] Unexpected packet type from spectator: {get_packet_type_name(ptype)}")
                    # Send an informative message about valid commands
                    if not send_packet(conn, PACKET_TYPE_CHAT, "As a spectator, you can use 'quit' to leave or send chat messages."):
                        print("[DEBUG] Failed to send help message")
                    continue
                    
            except socket.timeout:
                # This is expected due to our timeout
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

def handle_game_session(player1_conn, player2_conn, player1_addr, player2_addr, player1_username, player2_username):
    """
    Handle a game session between two connected players.
    Manages multiple games in succession if players choose to play again.
    When this function returns, the connections will be closed.
    """
    global game_in_progress, current_game_spectators, disconnected_players, current_game
    
    # Set socket timeouts for gameplay
    player1_conn.settimeout(CONNECTION_TIMEOUT)
    player2_conn.settimeout(CONNECTION_TIMEOUT)
    
    # Create protocol adapters
    player1_adapter = ProtocolAdapter(player1_conn, player1_username)
    player2_adapter = ProtocolAdapter(player2_conn, player2_username)
    
    # Avoiding circular imports - caused errors before
    from battleship import Board, BOARD_SIZE
    
    # Keep track of player boards for reconnection
    player1_board = None
    player2_board = None
    
    try:
        play_again = True
        while play_again:
            # Run a single game
            print("[INFO] Starting a new game between players...")
            notify_spectators("A new game is starting!")
            
            # Update the current game object for spectators
            current_game.game_state = "starting"
            current_game.last_move = None
            current_game.last_move_result = None
            
            # Send game start notification
            send_packet(player1_conn, PACKET_TYPE_GAME_START, f"Starting game against {player2_username}")
            send_packet(player2_conn, PACKET_TYPE_GAME_START, f"Starting game against {player1_username}")
            
            try:
                # Create new board objects for this game 
                player1_board = Board(BOARD_SIZE)
                player2_board = Board(BOARD_SIZE)
                
                # Update the current game object for spectators
                current_game.game_state = "setup"
                
                # Create a callback to update game state for spectators
                def update_game_state_for_spectators(move, result):
                    current_game.last_move = move
                    current_game.last_move_result = result
                    current_game.game_state = "in_progress"
                    # Notify spectators with the update
                    notify_spectators(f"Move: {move}, Result: {result}")
                
                # Run the game - create the board objects internally
                run_two_player_game(player1_adapter, player1_adapter, player2_adapter, player2_adapter, 
                                   notify_spectators_callback=notify_spectators)
                                   
                # Set game state to completed                   
                current_game.game_state = "completed"
                
            except ConnectionResetError:
                # Update game state
                current_game.game_state = "interrupted"
                
                # Handle disconnection during gameplay
                if player1_conn.fileno() == -1:  # Player 1 disconnected
                    # Store player1's board and game state for potential reconnection
                    with disconnected_players_lock:
                        disconnected_players[player1_username] = {
                            'disconnect_time': time.time(),
                            'opponent_username': player2_username,
                            'opponent_conn': player2_conn
                        }
                    
                    handle_player_disconnect(player1_conn, player1_username)
                    
                    # Notify player2 about reconnection window
                    try:
                        send_packet(player2_conn, PACKET_TYPE_CHAT, 
                                   f"\n{player1_username} has disconnected. Waiting {RECONNECT_TIMEOUT} seconds for reconnection...")
                    except:
                        pass
                        
                    notify_spectators(f"{player1_username} has disconnected. Waiting for reconnection...")
                    
                    # Wait for reconnection for RECONNECT_TIMEOUT seconds
                    reconnection_wait_start = time.time()
                    while time.time() - reconnection_wait_start < RECONNECT_TIMEOUT:
                        # Check if player1 has reconnected
                        with active_usernames_lock:
                            reconnected = player1_username in active_usernames
                            
                        if reconnected:
                            # Player has reconnected, update connection
                            with active_usernames_lock:
                                player1_conn = active_usernames[player1_username]
                                player1_adapter.conn = player1_conn
                                
                            print(f"[INFO] {player1_username} reconnected. Continuing game.")
                            send_packet(player2_conn, PACKET_TYPE_CHAT, f"{player1_username} has reconnected. Game continues.")
                            notify_spectators(f"{player1_username} has reconnected. Game continues.")
                            
                            # Update game state 
                            current_game.game_state = "in_progress"
                            
                            # Reset player's board from stored state
                            with disconnected_players_lock:
                                if player1_username in disconnected_players:
                                    del disconnected_players[player1_username]
                                    
                            break
                            
                        time.sleep(1)
                        
                    # If still not reconnected after timeout, player2 wins
                    with active_usernames_lock:
                        reconnected = player1_username in active_usernames
                        
                    if not reconnected:
                        current_game.game_state = "completed"
                        current_game.last_move_result = f"{player2_username} wins by default"
                        
                        try:
                            send_packet(player2_conn, PACKET_TYPE_CHAT, f"\n{player1_username} did not reconnect within the time limit. You win by default!")
                        except:
                            pass
                        notify_spectators(f"{player1_username} did not reconnect within the time limit. {player2_username} wins by default!")
                        break
                        
                else:  # Player 2 disconnected
                    # Store player2's board and game state for potential reconnection
                    with disconnected_players_lock:
                        disconnected_players[player2_username] = {
                            'disconnect_time': time.time(),
                            'opponent_username': player1_username,
                            'opponent_conn': player1_conn
                        }
                    
                    handle_player_disconnect(player2_conn, player2_username)
                    
                    # Notify player1 about reconnection window
                    try:
                        send_packet(player1_conn, PACKET_TYPE_CHAT, 
                                   f"\n{player2_username} has disconnected. Waiting {RECONNECT_TIMEOUT} seconds for reconnection...")
                    except:
                        pass
                        
                    notify_spectators(f"{player2_username} has disconnected. Waiting for reconnection...")
                    
                    # Wait for reconnection for RECONNECT_TIMEOUT seconds
                    reconnection_wait_start = time.time()
                    while time.time() - reconnection_wait_start < RECONNECT_TIMEOUT:
                        # Check if player2 has reconnected
                        with active_usernames_lock:
                            reconnected = player2_username in active_usernames
                            
                        if reconnected:
                            # Player has reconnected, update connection
                            with active_usernames_lock:
                                player2_conn = active_usernames[player2_username]
                                player2_adapter.conn = player2_conn
                                
                            print(f"[INFO] {player2_username} reconnected. Continuing game.")
                            send_packet(player1_conn, PACKET_TYPE_CHAT, f"{player2_username} has reconnected. Game continues.")
                            notify_spectators(f"{player2_username} has reconnected. Game continues.")
                            
                            # Reset player's board from stored state
                            with disconnected_players_lock:
                                if player2_username in disconnected_players:
                                    del disconnected_players[player2_username]
                                    
                            break
                            
                        time.sleep(1)
                        
                    # If still not reconnected after timeout, player1 wins
                    with active_usernames_lock:
                        reconnected = player2_username in active_usernames
                        
                    if not reconnected:
                        current_game.game_state = "completed"
                        current_game.last_move_result = f"{player1_username} wins by default"
                        
                        try:
                            send_packet(player1_conn, PACKET_TYPE_CHAT, f"\n{player2_username} did not reconnect within the time limit. You win by default!")
                        except:
                            pass
                        notify_spectators(f"{player2_username} did not reconnect within the time limit. {player1_username} wins by default!")
                        break
                        
            except BrokenPipeError:
                # Similar logic to ConnectionResetError - handle as disconnection
                if player1_conn.fileno() == -1:
                    handle_player_disconnect(player1_conn, player1_username)
                else:
                    handle_player_disconnect(player2_conn, player2_username)
                break
            except socket.timeout:
                # Handle timeout during gameplay
                print("[ERROR] Game session timed out")
                try:
                    send_packet(player1_conn, PACKET_TYPE_ERROR, "Game session timed out. Disconnecting...")
                except:
                    pass
                try:
                    send_packet(player2_conn, PACKET_TYPE_ERROR, "Game session timed out. Disconnecting...")
                except:
                    pass
                notify_spectators("Game session timed out. Game ending.")
                break
            except Exception as e:
                print(f"[ERROR] Unexpected error during gameplay: {e}")
                traceback.print_exc()
                notify_spectators(f"Game ended due to an error: {e}")
                break
            
            # Add a small delay to let players see the final result
            try:
                send_packet(player1_conn, PACKET_TYPE_CHAT, "Game over! Please wait...")
                send_packet(player2_conn, PACKET_TYPE_CHAT, "Game over! Please wait...")
                notify_spectators("Game over! Waiting for players to decide if they want to play again...")
            except:
                break
            
            # Add a delay to ensure players can see the final results
            time.sleep(3)  # 3 second delay
            
            # Ask players if they want to play again
            try:
                player1_wants_rematch = ask_play_again(player1_conn)
                player2_wants_rematch = ask_play_again(player2_conn)
            except:
                break
            
            # Only continue if both players want to play again
            if player1_wants_rematch and player2_wants_rematch:
                try:
                    send_packet(player1_conn, PACKET_TYPE_CHAT, "Both players have agreed to play again. Starting a new game...")
                    send_packet(player2_conn, PACKET_TYPE_CHAT, "Both players have agreed to play again. Starting a new game...")
                    notify_spectators("Both players have agreed to play again. Starting a new game...")
                    play_again = True
                except:
                    break
            else:
                # Inform players of the decision
                try:
                    if not player1_wants_rematch:
                        send_packet(player1_conn, PACKET_TYPE_CHAT, "You declined to play again. Ending session.")
                        send_packet(player2_conn, PACKET_TYPE_CHAT, "The other player declined to play again. Ending session.")
                        notify_spectators(f"{player1_username} declined to play again. Game ending.")
                    elif not player2_wants_rematch:
                        send_packet(player2_conn, PACKET_TYPE_CHAT, "You declined to play again. Ending session.")
                        send_packet(player1_conn, PACKET_TYPE_CHAT, "The other player declined to play again. Ending session.")
                        notify_spectators(f"{player2_username} declined to play again. Game ending.")
                    else:
                        send_packet(player1_conn, PACKET_TYPE_CHAT, "Session ending due to an unexpected error.")
                        send_packet(player2_conn, PACKET_TYPE_CHAT, "Session ending due to an unexpected error.")
                        notify_spectators("Game ending due to an unexpected error.")
                except:
                    pass
                play_again = False
        
        # Game session ended by player choice
        try:
            send_packet(player1_conn, PACKET_TYPE_GAME_END, "Thank you for playing! Disconnecting now.")
            send_packet(player2_conn, PACKET_TYPE_GAME_END, "Thank you for playing! Disconnecting now.")
            notify_spectators("Game has ended. Thank you for spectating!")
        except:
            pass
        print(f"[INFO] Game session ended between players at {player1_addr} and {player2_addr}")
    
    except socket.timeout:
        handle_player_disconnect(player1_conn, player1_username)
        handle_player_disconnect(player2_conn, player2_username)
        notify_spectators("Game ended due to a timeout.")
    except ConnectionResetError:
        handle_player_disconnect(player1_conn, player1_username)
        handle_player_disconnect(player2_conn, player2_username)
        notify_spectators("Game ended due to a connection reset.")
    except BrokenPipeError:
        handle_player_disconnect(player1_conn, player1_username)
        handle_player_disconnect(player2_conn, player2_username)
        notify_spectators("Game ended due to a broken connection.")
    except Exception as e:
        print(f"[ERROR] Game error: {e}\n{traceback.format_exc()}")
        handle_player_disconnect(player1_conn, player1_username)
        handle_player_disconnect(player2_conn, player2_username)
        notify_spectators(f"Game ended due to an error: {e}")
    finally:
        # Clean up player data
        with active_usernames_lock:
            if player1_username in active_usernames and active_usernames[player1_username] == player1_conn:
                del active_usernames[player1_username]
                print(f"[INFO] Cleaned up {player1_username} from active usernames")
            if player2_username in active_usernames and active_usernames[player2_username] == player2_conn:
                del active_usernames[player2_username]
                print(f"[INFO] Cleaned up {player2_username} from active usernames")
        
        with disconnected_players_lock:
            if player1_username in disconnected_players:
                del disconnected_players[player1_username]
                print(f"[INFO] Cleaned up {player1_username} from disconnected players")
            if player2_username in disconnected_players:
                del disconnected_players[player2_username]
                print(f"[INFO] Cleaned up {player2_username} from disconnected players")
        
        # Ensure connections are closed properly
        try:
            player1_conn.close()
            player2_conn.close()
            print("[INFO] Client connections closed. Ready for new players.")
        except:
            pass
        
        # Clear spectators list
        with spectators_lock:
            current_game_spectators.clear()
        
        # Mark game as ended
        with game_lock:
            game_in_progress = False

def run_game_server():
    """
    Main server loop that handles connections and starts games.
    """
    global waiting_players, waiting_players_lock, game_in_progress, current_game
    
    print(f"[INFO] Server listening on {HOST}:{PORT}")
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((HOST, PORT))
        server_socket.listen(5)  # Allow more pending connections for waiting lobby
        
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
                        # Handle reconnection
                        print(f"[INFO] {username} is attempting to reconnect")
                        if handle_reconnection(conn, addr, username):
                            # Reconnection successful, continue to next connection
                            continue
                        else:
                            # Failed to reconnect, close connection
                            try:
                                send_packet(conn, PACKET_TYPE_ERROR, "Failed to reconnect to game session.")
                            except:
                                pass
                            conn.close()
                            continue
                    else:
                        # Username in use
                        print(f"[WARNING] Username {username} is already in use. Closing connection.")
                        try:
                            send_packet(conn, PACKET_TYPE_ERROR, message)
                            time.sleep(0.1)
                        except:
                            pass
                        conn.close()
                        continue
                
                # Add username to active usernames
                with active_usernames_lock:
                    active_usernames[username] = conn

                # Check if a game is in progress
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
                                player1_conn, player1_addr, player1_username = waiting_players.get()
                                
                                print(f"[INFO] Found waiting player: {player1_username}@{player1_addr}. Starting game with {username}@{addr}.")
                                # Start game with these two players
                                game_in_progress = True
                                
                                # Update the global current_game object
                                current_game = RealGame(player1_username, username)
                                
                                threading.Thread(target=handle_game_session, 
                                              args=(player1_conn, conn, player1_addr, addr, player1_username, username),
                                              daemon=True).start()
                            else:
                                # Add to waiting queue
                                print(f"[INFO] No game in progress and no waiting players. Adding {username}@{addr} to waiting queue.")
                                threading.Thread(target=handle_waiting_player,
                                              args=(conn, addr, username),
                                              daemon=True).start()
                
            except KeyboardInterrupt:
                print("[INFO] Server shutting down by keyboard interrupt")
                break
            except Exception as e:
                print(f"[ERROR] Unexpected server error: {e}")
                continue

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
    Returns:
    - (True, None) if username is available
    - (False, "error message") if username is already in use
    - (False, "disconnected") if username belongs to a disconnected player
    """
    print(f"[DEBUG] Checking username availability for '{username}'")
    
    # Check if username is in disconnected players dictionary
    with disconnected_players_lock:
        if username in disconnected_players:
            print(f"[DEBUG] Found '{username}' in disconnected_players")
            disconnect_time = disconnected_players[username]['disconnect_time']
            elapsed = time.time() - disconnect_time
            
            if elapsed <= RECONNECT_TIMEOUT:
                print(f"[DEBUG] '{username}' is within reconnection window ({elapsed:.1f}s < {RECONNECT_TIMEOUT}s)")
                return (False, "disconnected")  # Special case for reconnection
            else:
                print(f"[DEBUG] '{username}' reconnection window expired, cleaning up")
                del disconnected_players[username]
    
    # Next check if username is already in active usernames
    with active_usernames_lock:
        if username in active_usernames:
            existing_conn = active_usernames.get(username)
            if not existing_conn:
                print(f"[DEBUG] Anomaly: '{username}' was in active_usernames set but not retrievable via .get(). Proceeding as if not active.")
            else:
                print(f"[DEBUG] Username '{username}' found in active_usernames. Verifying existing connection status.")
                is_connection_alive = False
                try:
                    if send_packet(existing_conn, PACKET_TYPE_HEARTBEAT, ""):
                        print(f"[DEBUG] Heartbeat sent to existing connection for '{username}' successfully. Connection appears active.")
                        is_connection_alive = True
                    else:
                        print(f"[DEBUG] Heartbeat to '{username}' failed (send_packet returned False). Treating as stale connection.")
                except (socket.error, BrokenPipeError, ConnectionResetError) as e:
                    print(f"[DEBUG] Socket error while sending heartbeat to '{username}': {e}. Treating as stale connection.")
                except Exception as e_other:
                    print(f"[DEBUG] Unexpected error sending heartbeat to '{username}': {e_other}. Treating as stale connection.")

                if is_connection_alive:
                    return (False, "Username already in use by another player.")
                else:
                    print(f"[DEBUG] Cleaning up stale/dead active connection for '{username}'.")
                    try:
                        existing_conn.close()
                    except Exception as e_close:
                        print(f"[DEBUG] Error closing stale connection for '{username}': {e_close}")
                    
                    if username in active_usernames and active_usernames.get(username) == existing_conn:
                        del active_usernames[username]
                    
                    with disconnected_players_lock:
                        # Preserve any existing game-specific data if this user was in a game.
                        player_data = disconnected_players.get(username, {}) 
                        player_data['disconnect_time'] = time.time()
                        disconnected_players[username] = player_data
                        print(f"[DEBUG] Marked '{username}' as disconnected after cleaning stale active entry. New connection can attempt to reconnect.")
                    return (False, "disconnected")
        else:
            print(f"[DEBUG] '{username}' is not in active_usernames")
    
    print(f"[DEBUG] '{username}' is available")
    return (True, None)

def handle_reconnection(conn, addr, username):
    """
    Handle a player reconnection attempt.
    """
    print(f"[DEBUG] Handling reconnection attempt for {username} from {addr}")
    
    with disconnected_players_lock:
        # If the player is not in the disconnected list, can't reconnect
        if username not in disconnected_players:
            print(f"[WARNING] {username} tried to reconnect but was not found in disconnected players")
            send_packet(conn, PACKET_TYPE_ERROR, "No active game found for reconnection.")
            return False
            
        player_data = disconnected_players[username]
        disconnect_time = player_data['disconnect_time']
        current_time = time.time()
        
        # Check if reconnection window has expired
        elapsed = current_time - disconnect_time
        if elapsed > RECONNECT_TIMEOUT:
            print(f"[WARNING] {username} reconnection window expired ({elapsed:.1f} seconds > {RECONNECT_TIMEOUT}s)")
            send_packet(conn, PACKET_TYPE_ERROR, f"Reconnection window expired ({RECONNECT_TIMEOUT} seconds).")
            del disconnected_players[username]
            return False
            
        # Reconnection is valid - remove from disconnected_players
        print(f"[INFO] {username} successfully reconnected to their game after {elapsed:.1f}s")
        
        # Add to active usernames
        with active_usernames_lock:
            if username in active_usernames:
                print(f"[WARNING] {username} was already in active_usernames during reconnection!")
                # Close the old connection if it exists
                try:
                    old_conn = active_usernames[username]
                    send_packet(old_conn, PACKET_TYPE_ERROR, "Another client has reconnected with your username.")
                    old_conn.close()
                except:
                    pass
            active_usernames[username] = conn
            print(f"[DEBUG] Added {username} back to active_usernames")
            
        # Send welcome back message
        send_packet(conn, PACKET_TYPE_RECONNECT, "Successfully reconnected to your game.")
        
        # Remove from disconnected players after successful reconnection
        del disconnected_players[username]
        
        return True

if __name__ == "__main__":
    main()