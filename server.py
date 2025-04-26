"""
server.py

Serves a Battleship game session to two connected clients.
Game logic is handled entirely on the server using battleship.py.
Client sends FIRE commands, and receives game feedback.
"""

import socket
import threading
from battleship import run_two_player_game

HOST = '127.0.0.1'
PORT = 5001

def main():
    print(f"[INFO] Server listening on {HOST}:{PORT}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, PORT))
        s.listen(2)  # Allow up to 2 pending connections
        
        print("[INFO] Waiting for Player 1 to connect...")
        player1_conn, player1_addr = s.accept()
        print(f"[INFO] Player 1 connected from {player1_addr}")
        
        print("[INFO] Waiting for Player 2 to connect...")
        player2_conn, player2_addr = s.accept()
        print(f"[INFO] Player 2 connected from {player2_addr}")
        
        print("[INFO] Starting two-player game...")
        
        try:
            # Create file-like objects for reading from and writing to both players
            player1_rfile = player1_conn.makefile('r')
            player1_wfile = player1_conn.makefile('w')
            player2_rfile = player2_conn.makefile('r')
            player2_wfile = player2_conn.makefile('w')
            
            # Run the two-player game
            run_two_player_game(player1_rfile, player1_wfile, player2_rfile, player2_wfile)
            
        except Exception as e:
            print(f"[ERROR] Game error: {e}")
        finally:
            # Ensure connections are closed properly when the game ends
            # (either by one player sinking all the other player's ships or by a player forfeiting)
            player1_conn.close()
            player2_conn.close()
            print("[INFO] Game ended. All clients disconnected.")

if __name__ == "__main__":
    main()