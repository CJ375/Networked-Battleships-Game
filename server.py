"""
server.py

Serves Battleship game sessions to connected clients (clients can change over time).
Game logic is handled entirely on the server using battleship.py.
Client sends FIRE commands, and receives game feedback.
Supports multiple games in sequence without restarting the server, both with the same players
or with entirely new connections.
"""

import socket
import threading
import traceback
import time
import queue
import select
from battleship import run_two_player_game

HOST = '127.0.0.1'
PORT = 5001
CONNECTION_TIMEOUT = 60  # seconds to wait for a connection

# Global variables for game state
game_in_progress = False
game_lock = threading.Lock()
waiting_players = queue.Queue()
waiting_players_lock = threading.Lock()
spectators = set()
spectators_lock = threading.Lock()

def broadcast_to_spectators(message, board1=None, board2=None):
    """
    Send a message and optional board updates to all spectators.
    """
    with spectators_lock:
        disconnected_spectators = set()
        for spectator in spectators:
            try:
                spectator[1].write(message + '\n')
                spectator[1].flush()
                
                # If boards are provided, send them
                if board1 is not None:
                    spectator[1].write("YOUR_GRID\n")
                    spectator[1].write("  " + " ".join(str(i + 1).rjust(2) for i in range(board1.size)) + '\n')
                    for r in range(board1.size):
                        row_label = chr(ord('A') + r)
                        row_str = " ".join(board1.display_grid[r][c] for c in range(board1.size))
                        spectator[1].write(f"{row_label:2} {row_str}\n")
                    spectator[1].write('\n')
                
                if board2 is not None:
                    spectator[1].write("OPPONENT_GRID\n")
                    spectator[1].write("  " + " ".join(str(i + 1).rjust(2) for i in range(board2.size)) + '\n')
                    for r in range(board2.size):
                        row_label = chr(ord('A') + r)
                        row_str = " ".join(board2.display_grid[r][c] for c in range(board2.size))
                        spectator[1].write(f"{row_label:2} {row_str}\n")
                    spectator[1].write('\n')
                
                spectator[1].flush()
            except:
                disconnected_spectators.add(spectator)
        
        # Remove disconnected spectators
        spectators.difference_update(disconnected_spectators)

def handle_spectator(conn, addr):
    """
    Handle a spectator connection.
    Sends game updates and manages the connection until the game ends.
    """
    global spectators, game_in_progress
    
    try:
        rfile = conn.makefile('r')
        wfile = conn.makefile('w')
        
        # Add to spectators list
        with spectators_lock:
            spectators.add((conn, wfile, addr))
        
        # Send welcome message
        wfile.write("\n[INFO] You are now spectating the game.\n")
        wfile.write("[INFO] You will see all game moves and results.\n")
        wfile.write("[INFO] Type 'quit' to stop spectating.\n")
        wfile.write("[INFO] Note: Your commands will be ignored as you are a spectator.\n")
        wfile.flush()
        
        # Keep connection alive while spectating
        while True:
            try:
                # Check if spectator wants to quit or send a command
                if rfile in select.select([rfile], [], [], 1)[0]:
                    line = rfile.readline().strip()
                    if line.lower() == 'quit':
                        with spectators_lock:
                            spectators.discard((conn, wfile, addr))
                        wfile.write("[INFO] You have stopped spectating.\n")
                        wfile.flush()
                        return
                    else:
                        # Ignore any other commands and notify spectator
                        wfile.write("[ERROR] As a spectator your commands are ignored.\n")
                        wfile.flush()
                
                # Check if game is still in progress
                with game_lock:
                    if not game_in_progress:
                        wfile.write("[INFO] The game has ended. Disconnecting...\n")
                        wfile.flush()
                        return
                
            except (ConnectionResetError, BrokenPipeError):
                print(f"[INFO] Spectator disconnected from {addr}")
                with spectators_lock:
                    spectators.discard((conn, wfile, addr))
                return
            except Exception as e:
                print(f"[ERROR] Error handling spectator: {e}")
                return
                
    except Exception as e:
        print(f"[ERROR] Error setting up spectator: {e}")
        try:
            conn.close()
        except:
            pass

def handle_player_disconnect(player_conn, player_wfile, player_name):
    """
    Handle a player disconnection during gameplay.
    Sends appropriate messages and closes the connection.
    """
    try:
        player_wfile.write(f"\n[ERROR] Connection lost. You have been disconnected from the game.\n")
        player_wfile.flush()
    except:
        pass
        
    try:
        player_conn.close()
    except:
        pass
        
    print(f"[INFO] {player_name} disconnected during gameplay")
    
    # Notify spectators of disconnection
    broadcast_to_spectators(f"[INFO] {player_name} has disconnected from the game.")

def ask_play_again(player_rfile, player_wfile):
    """
    Ask a player if they want to play again.
    Returns True if they want to play again, False otherwise.
    """
    try:
        player_wfile.write("Do you want to play again? (Y/N): \n")
        player_wfile.flush()
        response = player_rfile.readline().strip().upper()
        return response == 'Y' or response == 'YES'
    except Exception as e:
        print(f"Error asking player to play again: {e}")
        return False

def handle_waiting_player(conn, addr):
    """
    Handle a player in the waiting lobby.
    Sends waiting messages and manages the connection until a game slot is available.
    """
    global waiting_players, waiting_players_lock, game_in_progress
    
    try:
        #print(f"[DEBUG] Player {addr} entered waiting lobby")
        # Add player to waiting queue
        with waiting_players_lock:
            waiting_players.put((conn, addr))
            position = waiting_players.qsize()
            
        # Send initial waiting message
        wfile = conn.makefile('w')
        wfile.write(f"\n[INFO] You are in the waiting lobby. Position: {position}\n")
        wfile.write("[INFO] You will be matched with another player when the current game ends.\n")
        wfile.write("[INFO] Type 'quit' to leave the waiting lobby.\n")
        wfile.flush()
        #print(f"[DEBUG] Sent waiting message to {addr}")
        
        # Keep connection alive while waiting
        while True:
            try:
                # Check if player wants to quit
                rfile = conn.makefile('r')
                if rfile in select.select([rfile], [], [], 1)[0]:
                    line = rfile.readline().strip()
                    if line.lower() == 'quit':
                        #print(f"[DEBUG] Player {addr} quit waiting lobby")
                        with waiting_players_lock:
                            # Remove player from queue if they're still in it
                            temp_queue = queue.Queue()
                            while not waiting_players.empty():
                                player = waiting_players.get()
                                if player[0] != conn:  # Skip the quitting player
                                    temp_queue.put(player)
                            waiting_players = temp_queue
                        wfile.write("[INFO] You have left the waiting lobby.\n")
                        wfile.flush()
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
                        #print(f"[DEBUG] Player {addr} no longer in queue")
                        return
                
                # Send position update
                wfile.write(f"[INFO] Your position in queue: {position}\n")
                wfile.flush()
                
            except (ConnectionResetError, BrokenPipeError):
                print(f"[INFO] Waiting player disconnected from {addr}")
                #print(f"[DEBUG] Player {addr} disconnected from waiting lobby")
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

def handle_game_session(player1_conn, player2_conn, player1_addr, player2_addr):
    """
    Handle a game session between two connected players.
    Manages multiple games in succession if players choose to play again.
    When this function returns, the connections will be closed.
    """
    global game_in_progress, spectators
    
    try:
        #print(f"[DEBUG] Starting game session between {player1_addr} and {player2_addr}")
        # Set socket timeouts for gameplay
        player1_conn.settimeout(CONNECTION_TIMEOUT)
        player2_conn.settimeout(CONNECTION_TIMEOUT)
        
        # Create file-like objects for reading and writing
        player1_rfile = player1_conn.makefile('r')
        player1_wfile = player1_conn.makefile('w')
        player2_rfile = player2_conn.makefile('r')
        player2_wfile = player2_conn.makefile('w')
        
        # Send welcome messages
        #print(f"[DEBUG] Sending welcome messages to players")
        player1_wfile.write("\n[INFO] Game is starting! You are Player 1.\n")
        player1_wfile.flush()
        player2_wfile.write("\n[INFO] Game is starting! You are Player 2.\n")
        player2_wfile.flush()
        
        # Signal game start to both players
        #print(f"[DEBUG] Sending GAME_START signal to players")
        player1_wfile.write("GAME_START\n")
        player1_wfile.flush()
        player2_wfile.write("GAME_START\n")
        player2_wfile.flush()
        
        play_again = True
        while play_again:
            # Run a single game
            print("[INFO] Starting a new game between players...")
            broadcast_to_spectators("[INFO] A new game is starting!")
            try:
                # Run the game and get the boards for spectator updates
                player1_board, player2_board = run_two_player_game(
                    player1_rfile, player1_wfile, 
                    player2_rfile, player2_wfile,
                    broadcast_to_spectators  # Pass the broadcast function
                )
                
                # Send final board states to spectators
                broadcast_to_spectators(
                    "[INFO] Game has ended. Final board states:",
                    player1_board,
                    player2_board
                )
                
            except ConnectionResetError:
                # Handle disconnection during gameplay
                if player1_conn.fileno() == -1:  # Player 1 disconnected
                    handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
                    try:
                        player2_wfile.write("\n[INFO] Player 1 has disconnected. You win by default!\n")
                        player2_wfile.flush()
                    except:
                        pass
                else:  # Player 2 disconnected
                    handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
                    try:
                        player1_wfile.write("\n[INFO] Player 2 has disconnected. You win by default!\n")
                        player1_wfile.flush()
                    except:
                        pass
                break
            except BrokenPipeError:
                # Handle broken pipe (similar to connection reset)
                if player1_conn.fileno() == -1:
                    handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
                    try:
                        player2_wfile.write("\n[INFO] Player 1 has disconnected. You win by default!\n")
                        player2_wfile.flush()
                    except:
                        pass
                else:
                    handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
                    try:
                        player1_wfile.write("\n[INFO] Player 2 has disconnected. You win by default!\n")
                        player1_wfile.flush()
                    except:
                        pass
                break
            except socket.timeout:
                # Handle timeout during gameplay
                print("[ERROR] Game session timed out")
                broadcast_to_spectators("[ERROR] Game session timed out")
                try:
                    player1_wfile.write("\n[ERROR] Game session timed out. Disconnecting...\n")
                    player1_wfile.flush()
                except:
                    pass
                try:
                    player2_wfile.write("\n[ERROR] Game session timed out. Disconnecting...\n")
                    player2_wfile.flush()
                except:
                    pass
                break
            except Exception as e:
                print(f"[ERROR] Unexpected error during gameplay: {e}")
                broadcast_to_spectators(f"[ERROR] Unexpected error during gameplay: {e}")
                traceback.print_exc()
                break
            
            # Add a small delay to let players see the final result
            try:
                player1_wfile.write("Game over! Please wait...\n")
                player1_wfile.flush()
                player2_wfile.write("Game over! Please wait...\n")
                player2_wfile.flush()
                broadcast_to_spectators("Game over! Please wait...")
            except:
                break
            
            # Add a delay to ensure players can see the final results
            time.sleep(3)  # 3 second delay
            
            # Ask players if they want to play again
            try:
                player1_wants_rematch = ask_play_again(player1_rfile, player1_wfile)
                player2_wants_rematch = ask_play_again(player2_rfile, player2_wfile)
            except:
                break
            
            # Only continue if both players want to play again
            if player1_wants_rematch and player2_wants_rematch:
                try:
                    player1_wfile.write("Both players have agreed to play again. Starting a new game...\n")
                    player1_wfile.flush()
                    player2_wfile.write("Both players have agreed to play again. Starting a new game...\n")
                    player2_wfile.flush()
                    broadcast_to_spectators("Both players have agreed to play again. Starting a new game...")
                    play_again = True
                except:
                    break
            else:
                # Inform players of the decision
                try:
                    if not player1_wants_rematch:
                        player1_wfile.write("You declined to play again. Ending session.\n")
                        player1_wfile.flush()
                        player2_wfile.write("The other player declined to play again. Ending session.\n")
                        player2_wfile.flush()
                        broadcast_to_spectators("Player 1 declined to play again. Game ending.")
                    elif not player2_wants_rematch:
                        player2_wfile.write("You declined to play again. Ending session.\n")
                        player2_wfile.flush()
                        player1_wfile.write("The other player declined to play again. Ending session.\n")
                        player1_wfile.flush()
                        broadcast_to_spectators("Player 2 declined to play again. Game ending.")
                    else:
                        player1_wfile.write("Session ending due to an unexpected error.\n")
                        player1_wfile.flush()
                        player2_wfile.write("Session ending due to an unexpected error.\n")
                        player2_wfile.flush()
                        broadcast_to_spectators("Session ending due to an unexpected error.")
                except:
                    pass
                play_again = False
        
        # Game session ended by player choice
        try:
            player1_wfile.write("Thank you for playing! Disconnecting now.\n")
            player1_wfile.flush()
            player2_wfile.write("Thank you for playing! Disconnecting now.\n")
            player2_wfile.flush()
            broadcast_to_spectators("Game has ended. Thank you for spectating!")
        except:
            pass
        print(f"[INFO] Game session ended between players at {player1_addr} and {player2_addr}")
    
    except socket.timeout:
        handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
        handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
    except ConnectionResetError:
        handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
        handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
    except BrokenPipeError:
        handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
        handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
    except Exception as e:
        print(f"[ERROR] Game error: {e}\n{traceback.format_exc()}")
        handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
        handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
    finally:
        # Ensure connections are closed properly
        try:
            player1_conn.close()
            player2_conn.close()
            print("[INFO] Client connections closed. Ready for new players.")
        except:
            pass
        
        # Mark game as ended and clear spectators
        with game_lock:
            game_in_progress = False
        with spectators_lock:
            spectators.clear()

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
                
                # Check if a game is in progress
                with game_lock:
                    if game_in_progress:
                        # Game in progress, add as spectator
                        threading.Thread(target=handle_spectator,
                                      args=(conn, addr),
                                      daemon=True).start()
                    else:
                        # No game in progress, check waiting queue
                        with waiting_players_lock:
                            if waiting_players.qsize() >= 1:
                                # Get waiting player
                                player1_conn, player1_addr = waiting_players.get()
                                
                                # Set game state before starting the game
                                game_in_progress = True
                                
                                # Start game with these two players
                                threading.Thread(target=handle_game_session, 
                                              args=(player1_conn, conn, player1_addr, addr),
                                              daemon=True).start()
                            else:
                                # Add to waiting queue
                                threading.Thread(target=handle_waiting_player,
                                              args=(conn, addr),
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