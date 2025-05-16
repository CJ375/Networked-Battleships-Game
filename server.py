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
current_game_spectators = []  # List of (conn, wfile) tuples for spectators
spectators_lock = threading.Lock()

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
        rfile = conn.makefile('r')
        wfile = conn.makefile('w')
        
        # Add player to waiting queue
        with waiting_players_lock:
            waiting_players.put((conn, rfile, wfile, addr))
            position = waiting_players.qsize()
            
        # Send initial waiting message
        wfile.write(f"\n[INFO] You are in the waiting lobby. Position: {position}\n")
        wfile.write("[INFO] You will be matched with another player when the current game ends.\n")
        wfile.write("[INFO] Type 'quit' to leave the waiting lobby.\n")
        wfile.flush()
        
        # Keep connection alive while waiting
        while True:
            try:
                # Check if player wants to quit
                if rfile in select.select([rfile], [], [], 1)[0]:
                    line = rfile.readline().strip()
                    if line.lower() == 'quit':
                        with waiting_players_lock:
                            # Remove player from queue if they're still in
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
                        return
                
                # Send position update
                wfile.write(f"[INFO] Your position in queue: {position}\n")
                wfile.flush()
                
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
        rfile = conn.makefile('r')
        wfile = conn.makefile('w')
        
        # Add spectator to the list
        with spectators_lock:
            current_game_spectators.append((conn, wfile))
        
        # Send welcome message
        wfile.write("\n[INFO] You are now spectating the current game.\n")
        wfile.write("[INFO] You will see all game updates but cannot participate.\n")
        wfile.write("[INFO] Type 'quit' to stop spectating.\n")
        wfile.flush()
        
        # Keep connection alive while game is in progress
        while game_in_progress:
            try:
                # Check if spectator wants to quit
                if rfile in select.select([rfile], [], [], 1)[0]:
                    line = rfile.readline().strip()
                    if line.lower() == 'quit':
                        break
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
            if (conn, wfile) in current_game_spectators:
                current_game_spectators.remove((conn, wfile))
        try:
            conn.close()
        except:
            pass

def notify_spectators(message):
    """
    Send a message to all spectators.
    """
    with spectators_lock:
        for conn, wfile in current_game_spectators[:]:  # Copy list to avoid modification during iteration
            try:
                wfile.write(message + '\n')
                wfile.flush()
            except:
                # Remove disconnected spectator
                current_game_spectators.remove((conn, wfile))

def handle_game_session(player1_conn, player2_conn, player1_addr, player2_addr):
    """
    Handle a game session between two connected players.
    Manages multiple games in succession if players choose to play again.
    When this function returns, the connections will be closed.
    """
    global game_in_progress, current_game_spectators
    
    # Set socket timeouts for gameplay
    player1_conn.settimeout(CONNECTION_TIMEOUT)
    player2_conn.settimeout(CONNECTION_TIMEOUT)
    
    # Create file-like objects for reading and writing
    player1_rfile = player1_conn.makefile('r')
    player1_wfile = player1_conn.makefile('w')
    player2_rfile = player2_conn.makefile('r')
    player2_wfile = player2_conn.makefile('w')
    
    try:
        play_again = True
        while play_again:
            # Run a single game
            print("[INFO] Starting a new game between players...")
            notify_spectators("A new game is starting!")
            try:
                run_two_player_game(player1_rfile, player1_wfile, player2_rfile, player2_wfile, notify_spectators_callback=notify_spectators)
            except ConnectionResetError:
                # Handle disconnection during gameplay
                if player1_conn.fileno() == -1:  # Player 1 disconnected
                    handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
                    try:
                        player2_wfile.write("\n[INFO] Player 1 has disconnected. You win by default!\n")
                        player2_wfile.flush()
                    except:
                        pass
                    notify_spectators("Player 1 has disconnected. Player 2 wins by default!")
                else:  # Player 2 disconnected
                    handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
                    try:
                        player1_wfile.write("\n[INFO] Player 2 has disconnected. You win by default!\n")
                        player1_wfile.flush()
                    except:
                        pass
                    notify_spectators("Player 2 has disconnected. Player 1 wins by default!")
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
                    notify_spectators("Player 1 has disconnected. Player 2 wins by default!")
                else:
                    handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
                    try:
                        player1_wfile.write("\n[INFO] Player 2 has disconnected. You win by default!\n")
                        player1_wfile.flush()
                    except:
                        pass
                    notify_spectators("Player 2 has disconnected. Player 1 wins by default!")
                break
            except socket.timeout:
                # Handle timeout during gameplay
                print("[ERROR] Game session timed out")
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
                notify_spectators("Game session timed out. Game ending.")
                break
            except Exception as e:
                print(f"[ERROR] Unexpected error during gameplay: {e}")
                traceback.print_exc()
                notify_spectators(f"Game ended due to an error: {e}")
                break
            
            # Add a small delay to let players see the final result
            try:
                player1_wfile.write("Game over! Please wait...\n")
                player1_wfile.flush()
                player2_wfile.write("Game over! Please wait...\n")
                player2_wfile.flush()
                notify_spectators("Game over! Waiting for players to decide if they want to play again...")
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
                    notify_spectators("Both players have agreed to play again. Starting a new game...")
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
                        notify_spectators("Player 1 declined to play again. Game ending.")
                    elif not player2_wants_rematch:
                        player2_wfile.write("You declined to play again. Ending session.\n")
                        player2_wfile.flush()
                        player1_wfile.write("The other player declined to play again. Ending session.\n")
                        player1_wfile.flush()
                        notify_spectators("Player 2 declined to play again. Game ending.")
                    else:
                        player1_wfile.write("Session ending due to an unexpected error.\n")
                        player1_wfile.flush()
                        player2_wfile.write("Session ending due to an unexpected error.\n")
                        player2_wfile.flush()
                        notify_spectators("Game ending due to an unexpected error.")
                except:
                    pass
                play_again = False
        
        # Game session ended by player choice
        try:
            player1_wfile.write("Thank you for playing! Disconnecting now.\n")
            player1_wfile.flush()
            player2_wfile.write("Thank you for playing! Disconnecting now.\n")
            player2_wfile.flush()
            notify_spectators("Game has ended. Thank you for spectating!")
        except:
            pass
        print(f"[INFO] Game session ended between players at {player1_addr} and {player2_addr}")
    
    except socket.timeout:
        handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
        handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
        notify_spectators("Game ended due to a timeout.")
    except ConnectionResetError:
        handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
        handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
        notify_spectators("Game ended due to a connection reset.")
    except BrokenPipeError:
        handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
        handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
        notify_spectators("Game ended due to a broken connection.")
    except Exception as e:
        print(f"[ERROR] Game error: {e}\n{traceback.format_exc()}")
        handle_player_disconnect(player1_conn, player1_wfile, "Player 1")
        handle_player_disconnect(player2_conn, player2_wfile, "Player 2")
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
                                player1_conn, player1_rfile, player1_wfile, player1_addr = waiting_players.get()
                                
                                # Start game with these two players
                                game_in_progress = True
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