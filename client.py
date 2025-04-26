"""
client.py

Connects to a Battleship server for a two-player game.
This client handles both single-player and two-player modes:
- Receives and displays game boards and messages from the server
- Sends user commands for ship placement and firing coordinates
- Runs in a threaded mode to handle asynchronous server messages

The client uses threading to separate:
- One thread continuously reads from the socket and displays messages
- The main thread handles user input and sends it to the server
"""

import socket
import threading

HOST = '127.0.0.1'
PORT = 5001

# Flag (global) indicating if the client should stop running
running = True

def receive_messages(rfile):
    """
    Continuously receive and display messages from the server
    """
    
    global running

    while running:
        line = rfile.readline()
        if not line:
            print("[INFO] Server disconnected.")
            running = False
            break
            
        line = line.strip()
        
        if line == "GRID":
            # Begin reading of board lines
            print("\n[Board]")
            while True:
                board_line = rfile.readline()

                if not board_line or board_line.strip() == "":
                    break
                print(board_line.strip())
        elif line == "YOUR_GRID":
            # Display player's own grid with ships
            print("\n[Your Board]")
            while True:
                board_line = rfile.readline()
                if not board_line or board_line.strip() == "":
                    break
                print(board_line.strip())
        elif line == "OPPONENT_GRID":
            # Display opponent's grid (only hits/misses visible)
            print("\n[Opponent's Board]")
            while True:
                board_line = rfile.readline()
                if not board_line or board_line.strip() == "":
                    break
                print(board_line.strip())
        else:
            # Normal message
            print(line)
        

def main():
    global running

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, PORT))
        rfile = s.makefile('r')
        wfile = s.makefile('w')

        # Start a thread for receiving messages
        receive_thread = threading.Thread(target=receive_messages, args=(rfile,))
        receive_thread.daemon = True # This ensures that the thread will exit when the main thread exits
        receive_thread.start()

        try:
            # Main thread handles user input
            while running:
                user_input = input(">> ")

                if user_input.lower() == 'quit':
                    running = False
                    break
                
                # Send user input to server
                wfile.write(user_input + '\n')
                wfile.flush()

        except KeyboardInterrupt:
            print("\n[INFO] Client exiting.")

if __name__ == "__main__":
    main()