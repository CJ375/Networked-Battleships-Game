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

# Global variables for game state
game_in_progress = False
game_lock = threading.Lock()
waiting_players = queue.Queue()
waiting_players_lock = threading.Lock()
current_game_spectators = []  # List of (conn, player_info) tuples for spectators
spectators_lock = threading.Lock()


class ProtocolAdapter:
    def __init__(self, conn, username):
        self.conn = conn
        self.username = username
        self.buffer = []
        
    def readline(self):
        """Read a line from the buffer or wait for a new packet"""
        if self.buffer:
            return self.buffer.pop(0)
            
        valid, header, payload = receive_packet(self.conn, timeout=MOVE_TIMEOUT)
        if not valid or not payload:
            raise ConnectionResetError("Failed to receive packet")
            
        payload_str = payload.decode() if isinstance(payload, bytes) else payload
        magic, seq, packet_type, data_len = header
        
        if packet_type == PACKET_TYPE_MOVE:
            return payload_str + "\n"
        elif packet_type == PACKET_TYPE_DISCONNECT:
            raise ConnectionResetError("Player disconnected")
        else:
            # Return empty string for other packet types
            return "\n"
            
    def write(self, msg):
        """Write a message to be sent as a packet"""
        if msg.startswith("YOUR_GRID\n") or msg.startswith("OPPONENT_GRID\n") or msg.startswith("SPECTATOR_GRID\n"):
            # Queue grid updates to be sent as a single packet
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
    Sends appropriate messages and closes the connection.
    """
    try:
        send_packet(player_conn, PACKET_TYPE_ERROR, f"Connection lost. You have been disconnected from the game.")
    except:
        pass
        
    try:
        player_conn.close()
    except:
        pass
        
    print(f"[INFO] {player_name} disconnected during gameplay")

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
    
    try:
        # Add player to waiting queue
        with waiting_players_lock:
            waiting_players.put((conn, addr, username))
            position = waiting_players.qsize()
            
        # Send initial waiting message
        send_packet(conn, PACKET_TYPE_CHAT, f"\nYou are in the waiting lobby. Position: {position}")
        send_packet(conn, PACKET_TYPE_CHAT, "You will be matched with another player when the current game ends.")
        send_packet(conn, PACKET_TYPE_CHAT, "Type 'quit' to leave the waiting lobby.")
        
        # Keep connection alive while waiting
        while True:
            try:
                # Check for player input using protocol
                valid, header, payload = receive_packet(conn, timeout=1)
                
                if valid and payload:
                    payload_str = payload.decode() if isinstance(payload, bytes) else payload
                    magic, seq, packet_type, data_len = header
                    
                    if packet_type == PACKET_TYPE_CHAT and payload_str.lower() == 'quit':
                        with waiting_players_lock:
                            # Remove player from queue if they're still in
                            temp_queue = queue.Queue()
                            while not waiting_players.empty():
                                player = waiting_players.get()
                                if player[0] != conn:  # Skip the quitting player
                                    temp_queue.put(player)
                            waiting_players = temp_queue
                        send_packet(conn, PACKET_TYPE_CHAT, "You have left the waiting lobby.")
                        return
                    
                    elif packet_type == PACKET_TYPE_DISCONNECT:
                        # Handle player disconnect
                        with waiting_players_lock:
                            # Remove player from queue
                            temp_queue = queue.Queue()
                            while not waiting_players.empty():
                                player = waiting_players.get()
                                if player[0] != conn:
                                    temp_queue.put(player)
                            waiting_players = temp_queue
                        return
                
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
                        return
                
                # Send position update
                send_packet(conn, PACKET_TYPE_CHAT, f"Your position in queue: {position}")
                
                # Send heartbeat to keep connection alive
                send_packet(conn, PACKET_TYPE_HEARTBEAT, "")
                
            except (ConnectionResetError, BrokenPipeError):
                print(f"[INFO] Waiting player disconnected from {addr}")
                with waiting_players_lock:
                    # Remove disconnected player from queue
                    temp_queue = queue.Queue()
                    while not waiting_players.empty():
                        player = waiting_players.get()
                        if player[0] != conn:
                            temp_queue.put(player)
                    waiting_players = temp_queue
                return
            except Exception as e:
                print(f"[ERROR] Error handling waiting player: {e}")
                return
                
    except Exception as e:
        print(f"[ERROR] Error setting up waiting player: {e}")
        try:
            conn.close()
        except:
            pass

def handle_spectator(conn, addr):
    """
    Handle a spectator connection.
    Sends game updates to the spectator and manages their connection.
    """
    try:
        # Add spectator to the list
        with spectators_lock:
            current_game_spectators.append(conn)
        
        # Send welcome message
        send_packet(conn, PACKET_TYPE_CHAT, "\nYou are now spectating the current game.")
        send_packet(conn, PACKET_TYPE_CHAT, "You will see all game updates but cannot participate.")
        send_packet(conn, PACKET_TYPE_CHAT, "Type 'quit' to stop spectating.")
        
        # Keep connection alive while game is in progress
        while game_in_progress:
            try:
                # Check for spectator input using protocol
                valid, header, payload = receive_packet(conn, timeout=1)
                
                if valid and payload:
                    payload_str = payload.decode() if isinstance(payload, bytes) else payload
                    magic, seq, packet_type, data_len = header
                    
                    if packet_type == PACKET_TYPE_CHAT and payload_str.lower() == 'quit':
                        break
                    if packet_type == PACKET_TYPE_DISCONNECT:
                        break
                
                # Send heartbeat to keep connection alive
                send_packet(conn, PACKET_TYPE_HEARTBEAT, "")
                
            except (ConnectionResetError, BrokenPipeError):
                break
            except Exception as e:
                print(f"[ERROR] Error handling spectator: {e}")
                break
                
    except Exception as e:
        print(f"[ERROR] Error setting up spectator: {e}")
    finally:
        # Remove spectator from list and close connection
        with spectators_lock:
            if conn in current_game_spectators:
                current_game_spectators.remove(conn)
        try:
            conn.close()
        except:
            pass

def notify_spectators(message):
    """
    Send a message to all spectators.
    """
    with spectators_lock:
        for conn in current_game_spectators[:]:  # Copy list to avoid modification during iteration
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
    global game_in_progress, current_game_spectators
    
    # Set socket timeouts for gameplay
    player1_conn.settimeout(CONNECTION_TIMEOUT)
    player2_conn.settimeout(CONNECTION_TIMEOUT)
    
    # Create protocol adapters
    player1_adapter = ProtocolAdapter(player1_conn, player1_username)
    player2_adapter = ProtocolAdapter(player2_conn, player2_username)
    
    try:
        play_again = True
        while play_again:
            # Run a single game
            print("[INFO] Starting a new game between players...")
            notify_spectators("A new game is starting!")
            
            # Send game start notification
            send_packet(player1_conn, PACKET_TYPE_GAME_START, f"Starting game against {player2_username}")
            send_packet(player2_conn, PACKET_TYPE_GAME_START, f"Starting game against {player1_username}")
            
            try:
                run_two_player_game(player1_adapter, player1_adapter, player2_adapter, player2_adapter, notify_spectators_callback=notify_spectators)
            except ConnectionResetError:
                # Handle disconnection during gameplay
                if player1_conn.fileno() == -1:  # Player 1 disconnected
                    handle_player_disconnect(player1_conn, player1_username)
                    try:
                        send_packet(player2_conn, PACKET_TYPE_CHAT, f"\n{player1_username} has disconnected. You win by default!")
                    except:
                        pass
                    notify_spectators(f"{player1_username} has disconnected. {player2_username} wins by default!")
                else:  # Player 2 disconnected
                    handle_player_disconnect(player2_conn, player2_username)
                    try:
                        send_packet(player1_conn, PACKET_TYPE_CHAT, f"\n{player2_username} has disconnected. You win by default!")
                    except:
                        pass
                    notify_spectators(f"{player2_username} has disconnected. {player1_username} wins by default!")
                break
            except BrokenPipeError:
                # Handle broken pipe (similar to connection reset)
                if player1_conn.fileno() == -1:
                    handle_player_disconnect(player1_conn, player1_username)
                    try:
                        send_packet(player2_conn, PACKET_TYPE_CHAT, f"\n{player1_username} has disconnected. You win by default!")
                    except:
                        pass
                    notify_spectators(f"{player1_username} has disconnected. {player2_username} wins by default!")
                else:
                    handle_player_disconnect(player2_conn, player2_username)
                    try:
                        send_packet(player1_conn, PACKET_TYPE_CHAT, f"\n{player2_username} has disconnected. You win by default!")
                    except:
                        pass
                    notify_spectators(f"{player2_username} has disconnected. {player1_username} wins by default!")
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
    global waiting_players, waiting_players_lock, game_in_progress
    
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

                # Check if a game is in progress
                with game_lock:
                    if game_in_progress:
                        # Game in progress, add as spectator
                        print(f"[INFO] Game in progress. {username}@{addr} will be a spectator.")
                        threading.Thread(target=handle_spectator,
                                      args=(conn, addr), 
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

if __name__ == "__main__":
    main()