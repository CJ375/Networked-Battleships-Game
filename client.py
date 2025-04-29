"""
client.py

Connects to a Battleship server for a two-player game.
This client handles both single-player and two-player modes:
- Receives and displays game boards and messages from the server
- Sends user commands for ship placement and firing coordinates
- Runs in a threaded mode to handle asynchronous server messages
- Supports playing multiple games in succession without disconnecting
- Provides feedback about move timeouts
"""

import socket
import threading
import time

HOST = '127.0.0.1'
PORT = 5001

# Flag (global) indicating if the client should stop running
running = True

def receive_messages(conn, wfile):
    """
    Continuously read messages from the server and handle them appropriately.
    This function runs in a separate thread.
    """
    try:
        rfile = conn.makefile('r')
        #print("[DEBUG] Started receive_messages thread")
        while True:
            try:
                line = rfile.readline()
                if not line:
                    #print("\n[DEBUG] Received empty line from server")
                    print("\n[ERROR] Server disconnected unexpectedly")
                    break
                
                line = line.strip()
                if not line:
                    #print("[DEBUG] Received empty line (whitespace)")
                    continue
                
                #print(f"[DEBUG] Received message: {line}")
                
                # Check for special messages
                if line == "GAME_START":
                    #print("\n[DEBUG] Received GAME_START signal")
                    print("\n[INFO] Game is starting!")
                    continue
                
                # Handle board messages
                if line.startswith("BOARD:"):
                    #print("[DEBUG] Received board update")
                    # Extract the board string
                    board_str = line[6:]  # Remove "BOARD:" prefix
                    # Print the board
                    print("\n" + board_str)
                    continue
                
                # Handle normal messages
                print(line)
                
            except ConnectionResetError:
                #print("\n[DEBUG] ConnectionResetError in receive_messages")
                print("\n[ERROR] Server disconnected unexpectedly")
                break
            except BrokenPipeError:
                #print("\n[DEBUG] BrokenPipeError in receive_messages")
                print("\n[ERROR] Server disconnected unexpectedly")
                break
            except socket.timeout:
                #print("[DEBUG] Socket timeout in receive_messages")
                continue
            except Exception as e:
                #print(f"\n[DEBUG] Exception in receive_messages: {e}")
                print(f"\n[ERROR] Error receiving message: {e}")
                break
                
    except Exception as e:
        #print(f"\n[DEBUG] Outer exception in receive_messages: {e}")
        print(f"\n[ERROR] Error in receive_messages: {e}")
    finally:
        try:
            #print("[DEBUG] Closing connection in receive_messages")
            conn.close()
        except:
            pass

def main():
    global running

    print("[INFO] Connecting to Battleship server...")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((HOST, PORT))
            print(f"[INFO] Connected to server at {HOST}:{PORT}")
            
            rfile = s.makefile('r')
            wfile = s.makefile('w')

            # Start a thread for receiving messages
            receive_thread = threading.Thread(target=receive_messages, args=(s, wfile))
            receive_thread.daemon = True # This ensures that the thread will exit when the main thread exits
            receive_thread.start()

            try:
                # Main thread handles user input
                while running:
                    try:
                        user_input = input(">> ")

                        if user_input.lower() == 'quit':
                            print("[INFO] Quitting the game...")
                            running = False
                            break
                        
                        # Send user input to server
                        try:
                            wfile.write(user_input + '\n')
                            wfile.flush()
                        except ConnectionResetError:
                            print("\n[ERROR] Connection to server was reset while sending command. Please restart the client.")
                            running = False
                            break
                        except BrokenPipeError:
                            print("\n[ERROR] Connection to server was broken while sending command. Please restart the client.")
                            running = False
                            break
                        except Exception as e:
                            print(f"\n[ERROR] Failed to send command to server: {e}")
                            running = False
                            break

                    except KeyboardInterrupt:
                        print("\n[INFO] Client exiting due to keyboard interrupt.")
                        running = False
                        break
                    except EOFError:
                        print("\n[INFO] End of input reached. Exiting...")
                        running = False
                        break
                    except Exception as e:
                        print(f"\n[ERROR] Unexpected error: {e}")
                        running = False
                        break

            except KeyboardInterrupt:
                print("\n[INFO] Client exiting due to keyboard interrupt.")
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