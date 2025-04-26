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
from battleship import run_two_player_game

HOST = '127.0.0.1'
PORT = 5001
CONNECTION_TIMEOUT = 60  # seconds to wait for a connection

def handle_client_disconnect(player1_conn, player2_conn, message):
    """
    Handle unexpected client disconnections by closing both connections
    and logging appropriate messages.
    """
    print(f"[ERROR] {message}")
    try:
        if player1_conn:
            player1_conn.close()
    except:
        pass
        
    try:
        if player2_conn:
            player2_conn.close()
    except:
        pass
    
    print("[INFO] All connections closed due to error.")

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

def run_game_server():
    """
    Main server loop that handles connections and starts games.
    """
    print(f"[INFO] Server listening on {HOST}:{PORT}")
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((HOST, PORT))
        server_socket.listen(2)  # Allow up to 2 pending connections
        
        while True:
            player1_conn = None
            player2_conn = None
            
            try:
                # Set a timeout for accepting connections
                server_socket.settimeout(CONNECTION_TIMEOUT)
                
                print("[INFO] Waiting for Player 1 to connect...")
                try:
                    player1_conn, player1_addr = server_socket.accept()
                    print(f"[INFO] Player 1 connected from {player1_addr}")
                except socket.timeout:
                    print("[ERROR] Timed out waiting for Player 1 to connect.")
                    continue
                    
                print("[INFO] Waiting for Player 2 to connect...")
                try:
                    player2_conn, player2_addr = server_socket.accept()
                    print(f"[INFO] Player 2 connected from {player2_addr}")
                except socket.timeout:
                    print("[ERROR] Timed out waiting for Player 2 to connect.")
                    player1_conn.close()
                    continue
                
                # Start a game session with these two players
                handle_game_session(player1_conn, player2_conn, player1_addr, player2_addr)
                
                # After handle_game_session returns, the connections are closed
                # and the server is ready to accept new connections
                print("[INFO] Server is ready for new connections.")
                
            except KeyboardInterrupt:
                print("[INFO] Server shutting down by keyboard interrupt")
                if player1_conn:
                    player1_conn.close()
                if player2_conn:
                    player2_conn.close()
                break
            except Exception as e:
                print(f"[ERROR] Unexpected server error: {e}")
                handle_client_disconnect(player1_conn, player2_conn, f"Server error: {e}")
                continue

def handle_game_session(player1_conn, player2_conn, player1_addr, player2_addr):
    """
    Handle a game session between two connected players.
    Manages multiple games in succession if players choose to play again.
    When this function returns, the connections will be closed.
    """
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
            run_two_player_game(player1_rfile, player1_wfile, player2_rfile, player2_wfile)
            
            # Add a small delay to let players see the final result
            player1_wfile.write("Game over! Please wait...\n")
            player1_wfile.flush()
            player2_wfile.write("Game over! Please wait...\n")
            player2_wfile.flush()
            
            # Add a delay to ensure players can see the final results
            time.sleep(3)  # 3 second delay
            
            # Ask players if they want to play again
            player1_wants_rematch = ask_play_again(player1_rfile, player1_wfile)
            player2_wants_rematch = ask_play_again(player2_rfile, player2_wfile)
            
            # Only continue if both players want to play again
            if player1_wants_rematch and player2_wants_rematch:
                player1_wfile.write("Both players have agreed to play again. Starting a new game...\n")
                player1_wfile.flush()
                player2_wfile.write("Both players have agreed to play again. Starting a new game...\n")
                player2_wfile.flush()
                play_again = True
            else:
                # Inform players of the decision
                if not player1_wants_rematch:
                    player1_wfile.write("You declined to play again. Ending session.\n")
                    player1_wfile.flush()
                    player2_wfile.write("The other player declined to play again. Ending session.\n")
                    player2_wfile.flush()
                elif not player2_wants_rematch:
                    player2_wfile.write("You declined to play again. Ending session.\n")
                    player2_wfile.flush()
                    player1_wfile.write("The other player declined to play again. Ending session.\n")
                    player1_wfile.flush()
                else:
                    player1_wfile.write("Session ending due to an unexpected error.\n")
                    player1_wfile.flush()
                    player2_wfile.write("Session ending due to an unexpected error.\n")
                    player2_wfile.flush()
                play_again = False
        
        # Game session ended by player choice
        player1_wfile.write("Thank you for playing! Disconnecting now.\n")
        player1_wfile.flush()
        player2_wfile.write("Thank you for playing! Disconnecting now.\n")
        player2_wfile.flush()
        print(f"[INFO] Game session ended between players at {player1_addr} and {player2_addr}")
    
    except socket.timeout:
        handle_client_disconnect(player1_conn, player2_conn, "Connection timed out during gameplay")
    except ConnectionResetError:
        handle_client_disconnect(player1_conn, player2_conn, "A client forcibly closed the connection")
    except BrokenPipeError:
        handle_client_disconnect(player1_conn, player2_conn, "Connection to a client was broken unexpectedly")
    except Exception as e:
        handle_client_disconnect(player1_conn, player2_conn, f"Game error: {e}\n{traceback.format_exc()}")
    finally:
        # Ensure connections are closed properly
        try:
            player1_conn.close()
            player2_conn.close()
            print("[INFO] Client connections closed. Ready for new players.")
        except:
            pass

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