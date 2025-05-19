"""
client.py

Connects to a Battleship server for a two-player game.
This client handles both single-player and two-player modes:
- Receives and displays game boards and messages from the server
- Sends user commands for ship placement and firing coordinates
- Runs in a threaded mode to handle asynchronous server messages
- Supports playing multiple games in succession without disconnecting
- Provides feedback about move timeouts
- Uses custom protocol
- Supports reconnection to an interrupted game
"""

import socket
import threading
import time
import os
import json
import tkinter as tk
from tkinter import scrolledtext, simpledialog, messagebox, PhotoImage
from queue import Queue
import re
from protocol import (
    receive_packet, send_packet, 
    PACKET_TYPE_USERNAME, PACKET_TYPE_MOVE, PACKET_TYPE_CHAT,
    PACKET_TYPE_DISCONNECT, PACKET_TYPE_RECONNECT, PACKET_TYPE_HEARTBEAT,
    PACKET_TYPE_GAME_START, PACKET_TYPE_BOARD_UPDATE, PACKET_TYPE_GAME_END,
    PACKET_TYPE_ERROR, PACKET_TYPE_ACK, get_packet_type_name
)

HOST = '127.0.0.1'
PORT = 5001
GUI_UPDATE_INTERVAL = 100

current_username = ""
is_spectator = False

project_root = os.getcwd() 
battleship_dir = os.path.join(project_root, ".reconnection_data")
os.makedirs(battleship_dir, exist_ok=True)

def get_connection_file(username):
    if not username:
        return None
    return os.path.join(battleship_dir, f".battleship_connection_{username}.json")

def save_connection_info(username):
    if not username:
        return
    connection_file = get_connection_file(username)
    try:
        with open(connection_file, 'w') as f:
            json.dump({
                'username': username, 
                'timestamp': time.time(),
                'disconnected': True 
            }, f)
    except Exception as e:
        print(f"[WARNING] Could not save connection information: {e}")
        pass

def mark_connection_active(username):
    if not username:
        return
    connection_file = get_connection_file(username)
    try:
        if not os.path.exists(connection_file):
            with open(connection_file, 'w') as f:
                json.dump({'username': username, 'timestamp': time.time(), 'disconnected': False}, f)
        else:
            # File exists, read, update, and write
            with open(connection_file, 'r+') as f:
                try:
                    data = json.load(f)
                    data['disconnected'] = False
                    data['timestamp'] = time.time()
                    f.seek(0)
                    json.dump(data, f)
                    f.truncate()
                except json.JSONDecodeError:
                    f.seek(0)
                    json.dump({'username': username, 'timestamp': time.time(), 'disconnected': False}, f)
                    f.truncate()
    except Exception as e:
        pass


def load_connection_info(username): # This will be used by the GUI to check
    if not username:
        return False
    connection_file = get_connection_file(username)
    try:
        if os.path.exists(connection_file):
            with open(connection_file, 'r') as f:
                data = json.load(f)
                elapsed = time.time() - data.get('timestamp', 0)
                if elapsed <= 60 and data.get('disconnected', False): # Only care if marked disconnected
                    return True # Let the GUI present this
                elif elapsed > 60:
                    try:
                        os.remove(connection_file) # Clean up old files
                    except:
                        pass
    except Exception as e:
        # print(f"[DEBUG] Error loading connection info: {e}")
        pass
    return False

def check_any_recent_connections(): # GUI will use this to present options
    recent_usernames = []
    try:
        for filename in os.listdir(battleship_dir):
            if filename.startswith(".battleship_connection_") and filename.endswith(".json"):
                connection_file = os.path.join(battleship_dir, filename)
                try:
                    with open(connection_file, 'r') as f:
                        data = json.load(f)
                        username = data.get('username')
                        timestamp = data.get('timestamp', 0)
                        disconnected = data.get('disconnected', False)
                        elapsed = time.time() - timestamp
                        if elapsed <= 60 and username and disconnected:
                            recent_usernames.append((username, elapsed))
                except:
                    continue
    except Exception as e:
        # print(f"[DEBUG] Error checking recent connections: {e}")
        pass
    recent_usernames.sort(key=lambda x: x[1])
    return recent_usernames

class BattleshipGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Battleship Client")
        self.geometry("1100x750")

        self.sock = None
        self.server_message_queue = Queue()
        self.network_thread = None
        self.username = ""
        self.is_spectator = False
        self.running = True

        self.spectator_player1_username = None
        self.spectator_player2_username = None

        # Board and cell dimensions
        self.board_size = 10
        self.cell_size = 30

        # Ship placement state
        self.is_placing_ships = False
        self.ships_to_place_list = [] # List of tuples (ship_name, ship_length)
        self.current_ship_to_place_idx = 0
        self.current_ship_name = ""
        self.current_ship_length = 0
        self.selected_placement_coord = None # e.g. "A1"
        self.placement_orientation_var = tk.StringVar(value="H") # Default to Horizontal

        self._setup_ui()
        self._prompt_for_username_and_connect()
        
        # Add a protocol to handle window close
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _setup_ui(self):
        main_frame = tk.Frame(self)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Create for game area and chat area
        self.paned_window = tk.PanedWindow(main_frame, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=5)
        self.paned_window.pack(fill=tk.BOTH, expand=True)

        self.game_area_frame = tk.Frame(self.paned_window, relief=tk.SUNKEN, borderwidth=1)
        self.paned_window.add(self.game_area_frame, width=750)

        # Boards frame
        self.boards_frame = tk.Frame(self.game_area_frame)
        self.boards_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Player Board UI
        self.player_board_frame = tk.Frame(self.boards_frame, relief=tk.SUNKEN, borderwidth=1)
        self.player_board_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.player_board_label = tk.Label(self.player_board_frame, text="Your Board")
        self.player_board_label.pack()
        self.player_board_canvas = tk.Canvas(self.player_board_frame, 
                                             width=self.cell_size * (self.board_size + 1), 
                                             height=self.cell_size * (self.board_size + 1), 
                                             bg="lightblue")
        self.player_board_canvas.pack(pady=5)
        self.player_board_canvas.bind("<Button-1>", self._on_player_board_click)

        # Opponent Board UI
        self.opponent_board_frame = tk.Frame(self.boards_frame, relief=tk.SUNKEN, borderwidth=1)
        self.opponent_board_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.opponent_board_name_label = tk.Label(self.opponent_board_frame, text="Opponent's Board")
        self.opponent_board_name_label.pack()
        self.opponent_board_canvas = tk.Canvas(self.opponent_board_frame, 
                                               width=self.cell_size * (self.board_size + 1), 
                                               height=self.cell_size * (self.board_size + 1), 
                                               bg="lightcoral")
        self.opponent_board_canvas.pack(pady=5)
        self.opponent_board_canvas.bind("<Button-1>", self._on_opponent_board_click)
        
        # Ship Placement UI
        self.placement_frame = tk.Frame(self.game_area_frame, pady=10)

        self.placement_prompt_label = tk.Label(self.placement_frame, text="Ship Placement Options:")
        self.placement_prompt_label.pack()
        
        self.manual_random_frame = tk.Frame(self.placement_frame) 
        tk.Button(self.manual_random_frame, text="Place Manually", command=lambda: self._send_placement_choice("M")).pack(side=tk.LEFT, padx=5)
        tk.Button(self.manual_random_frame, text="Place Randomly", command=lambda: self._send_placement_choice("R")).pack(side=tk.LEFT, padx=5)

        self.current_ship_label = tk.Label(self.placement_frame, text="Placing: None")
        self.current_ship_label.pack(pady=2)
        self.selected_coord_label = tk.Label(self.placement_frame, text="Selected Start: None")
        self.selected_coord_label.pack(pady=2)
        orientation_frame = tk.Frame(self.placement_frame)
        orientation_frame.pack()
        tk.Label(orientation_frame, text="Orientation:").pack(side=tk.LEFT)
        tk.Radiobutton(orientation_frame, text="Horizontal", variable=self.placement_orientation_var, value="H").pack(side=tk.LEFT)
        tk.Radiobutton(orientation_frame, text="Vertical", variable=self.placement_orientation_var, value="V").pack(side=tk.LEFT)
        self.confirm_placement_button = tk.Button(self.placement_frame, text="Confirm Ship Placement", command=self._confirm_ship_placement_action)
        self.confirm_placement_button.pack(pady=5)

        # Draw grid lines on canvases
        self.draw_grid_lines(self.player_board_canvas)
        self.draw_grid_lines(self.opponent_board_canvas)
        
        self.chat_area_frame = tk.Frame(self.paned_window, relief=tk.SUNKEN, borderwidth=1)
        self.paned_window.add(self.chat_area_frame, minsize=250) # Min width for chat area

        # Chat/Log display
        self.chat_display = scrolledtext.ScrolledText(self.chat_area_frame, height=10, state=tk.DISABLED, wrap=tk.WORD)
        self.chat_display.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Input field and send button
        input_frame = tk.Frame(self.chat_area_frame)
        input_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)
        self.input_field = tk.Entry(input_frame)
        self.input_field.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.input_field.bind("<Return>", self._send_input)
        self.send_button = tk.Button(input_frame, text="Send Chat", command=self._send_input)
        self.send_button.pack(side=tk.RIGHT)

    def _toggle_ship_placement_ui(self, show=False, show_mr_choice=False):
        if show:
            self.placement_frame.pack(side=tk.TOP, fill=tk.X, pady=10, after=self.boards_frame)
            if show_mr_choice:
                self.manual_random_frame.pack(pady=5)
                self.current_ship_label.pack_forget()
                self.selected_coord_label.pack_forget()
                
                for child in self.placement_frame.winfo_children():
                    if any(isinstance(grandchild, tk.Radiobutton) for grandchild in child.winfo_children()):
                        child.pack_forget()
                        break
                self.confirm_placement_button.pack_forget()
            else:
                self.manual_random_frame.pack_forget()
                self.current_ship_label.pack()
                self.selected_coord_label.pack()

                orientation_frame_found = False
                for child in self.placement_frame.winfo_children():
                    if any(isinstance(grandchild, tk.Radiobutton) for grandchild in child.winfo_children()):
                        child.pack()
                        orientation_frame_found = True
                        break
                self.confirm_placement_button.pack()
        else:
            self.placement_frame.pack_forget()
            self.manual_random_frame.pack_forget()
        self.is_placing_ships = show and not show_mr_choice

    def draw_grid_lines(self, canvas):
        # Draw column labels (1-10)
        for i in range(self.board_size):
            x = (i + 1.5) * self.cell_size 
            y = self.cell_size / 2
            canvas.create_text(x, y, text=str(i + 1))

        # Draw row labels (A-J)
        for i in range(self.board_size):
            x = self.cell_size / 2
            y = (i + 1.5) * self.cell_size
            canvas.create_text(x, y, text=chr(ord('A') + i))
        
        grid_origin_x = self.cell_size
        grid_origin_y = self.cell_size

        for i in range(self.board_size + 1):
            # Vertical lines
            x0, y0 = grid_origin_x + i * self.cell_size, grid_origin_y
            x1, y1 = grid_origin_x + i * self.cell_size, grid_origin_y + self.board_size * self.cell_size
            canvas.create_line(x0, y0, x1, y1)
            # Horizontal lines
            x0, y0 = grid_origin_x, grid_origin_y + i * self.cell_size
            x1, y1 = grid_origin_x + self.board_size * self.cell_size, grid_origin_y + i * self.cell_size
            canvas.create_line(x0, y0, x1, y1)
        
        canvas.config(width=self.cell_size * (self.board_size + 1), height=self.cell_size * (self.board_size + 1))

    def _canvas_coord_to_grid_coord(self, event_x, event_y):
        grid_origin_x = self.cell_size
        grid_origin_y = self.cell_size
        
        # Check if click is outside grid area (but within labeled area)
        if event_x < grid_origin_x or event_y < grid_origin_y:
            return None 
        if event_x > grid_origin_x + self.board_size * self.cell_size or \
           event_y > grid_origin_y + self.board_size * self.cell_size:
            return None

        col = int((event_x - grid_origin_x) / self.cell_size)
        row = int((event_y - grid_origin_y) / self.cell_size)

        if 0 <= row < self.board_size and 0 <= col < self.board_size:
            return f"{chr(ord('A') + row)}{col + 1}"
        return None

    def _on_opponent_board_click(self, event):
        if self.is_spectator or self.is_placing_ships: # Don't fire if spectator or placing ships
            return

        coord = self._canvas_coord_to_grid_coord(event.x, event.y)
        if coord and self.sock:
            self.log_message(f"[ACTION] Firing at {coord} on opponent's board (from click).")
            send_packet(self.sock, PACKET_TYPE_MOVE, coord) 

    def _on_player_board_click(self, event):
        if not self.is_placing_ships or self.is_spectator:
            return

        coord = self._canvas_coord_to_grid_coord(event.x, event.y)
        if coord:
            self.selected_placement_coord = coord
            self.selected_coord_label.config(text=f"Selected Start: {coord}")
            self.log_message(f"[PLACEMENT] Selected starting cell: {coord} for {self.current_ship_name}")

    def _prompt_manual_or_random_placement(self):
        self.log_message("[SERVER] Would you like to place ships manually (M) or randomly (R)?")
        self._toggle_ship_placement_ui(show=True, show_mr_choice=True)

    def _send_placement_choice(self, choice): # "M" or "R"
        if self.sock:
            send_packet(self.sock, PACKET_TYPE_MOVE, choice)
            self.log_message(f"[ACTION] Sent placement choice: {choice}")
            self._toggle_ship_placement_ui(show=False) # Hide M/R choice UI
            if choice.upper() == "M":
                 self.log_message("[INFO] Waiting for server to send ship details for manual placement...")

    def _start_manual_ship_placement(self, ships_string_from_server):
        self.log_message(f"[INFO] Starting manual ship placement. Server says: {ships_string_from_server}")
        
        match = re.search(r"([A-Za-z\s]+)\s*\((\d+)\s*cells?\)", ships_string_from_server)
        if match:
            self.current_ship_name = match.group(1).strip()
            self.current_ship_length = int(match.group(2))
            self.log_message(f"[PLACEMENT] Now placing: {self.current_ship_name} (Length: {self.current_ship_length})")
            self.current_ship_label.config(text=f"Placing: {self.current_ship_name} ({self.current_ship_length} cells)")
            self.selected_coord_label.config(text="Selected Start: None")
            self.selected_placement_coord = None
            self._toggle_ship_placement_ui(show=True, show_mr_choice=False)
        else:
            self.log_message(f"[ERROR] Could not parse ship details from server: {ships_string_from_server}")
            self._toggle_ship_placement_ui(show=False)


    def _confirm_ship_placement_action(self):
        if not self.selected_placement_coord:
            messagebox.showwarning("Placement Error", "Please select a starting cell on your board.", parent=self)
            return
        if not self.current_ship_name:
            messagebox.showerror("Placement Error", "No current ship to place. Waiting for server.", parent=self)
            return

        orientation = self.placement_orientation_var.get()
        placement_command = f"{self.selected_placement_coord} {orientation}"
        
        self.log_message(f"[ACTION] Sending placement for {self.current_ship_name}: {placement_command}")
        if self.sock:
            send_packet(self.sock, PACKET_TYPE_MOVE, placement_command)
            
        self.selected_coord_label.config(text="Selected Start: Waiting...")


    def _prompt_for_username_and_connect(self):
        global current_username 

        recent_connections = check_any_recent_connections()
        chosen_username = None

        if recent_connections:
            options = []
            for i, (uname, elapsed) in enumerate(recent_connections):
                options.append(f"{uname} (disconnected {elapsed:.0f}s ago)")
            
            dialog = ReconnectionDialog(self, "Reconnect?", options)
            choice_index = dialog.choice
            
            if choice_index is not None and 0 <= choice_index < len(recent_connections):
                chosen_username = recent_connections[choice_index][0]
                self.log_message(f"[INFO] Attempting to reconnect as '{chosen_username}'...")
            elif choice_index == -1: 
                self.log_message("[INFO] Proceeding with a new connection.")
                for uname_to_delete, _ in recent_connections:
                    try:
                        os.remove(get_connection_file(uname_to_delete))
                    except:
                        pass 
                chosen_username = simpledialog.askstring("Username", "Enter your username:", parent=self)
            else:
                self.log_message("[INFO] No reconnection selected or dialog cancelled. Please enter a username.")
                chosen_username = simpledialog.askstring("Username", "Enter your username:", parent=self)
        else:
            chosen_username = simpledialog.askstring("Username", "Enter your username:", parent=self)

        if not chosen_username:
            self.log_message("[ERROR] Username cannot be empty. Exiting.")
            messagebox.showerror("Error", "Username cannot be empty. The application will now close.")
            self.destroy()
            return

        self.username = chosen_username
        current_username = self.username 

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((HOST, PORT))
            self.log_message(f"[INFO] Connected to server at {HOST}:{PORT}")

            if send_packet(self.sock, PACKET_TYPE_USERNAME, self.username):
                self.log_message(f"[INFO] Username '{self.username}' sent to server.")
                save_connection_info(self.username) 
                mark_connection_active(self.username) 
                
                self.network_thread = threading.Thread(target=self._receive_messages_thread, daemon=True)
                self.network_thread.start()
                
                self.after(GUI_UPDATE_INTERVAL, self._process_gui_queue)
                
                self.log_message("[INFO] Waiting for server response...")
                self.log_message("[INFO] You may be placed as a player or spectator.")
                self.log_message("[INFO] Type messages in the input field below and press Enter or Send Chat.")
                self.log_message("[INFO] Click on opponent's board to fire. Follow prompts for ship placement.")


            else:
                self.log_message("[ERROR] Failed to send username to server.")
                messagebox.showerror("Connection Error", "Failed to send username to server.")
                if self.sock: self.sock.close()
                self.destroy()
        except ConnectionRefusedError:
            self.log_message(f"[ERROR] Could not connect to server at {HOST}:{PORT}. Check if server is running.")
            messagebox.showerror("Connection Error", f"Could not connect to server at {HOST}:{PORT}.\\nCheck if the server is running.")
            self.destroy()
        except Exception as e:
            self.log_message(f"[ERROR] Connection error: {e}")
            messagebox.showerror("Connection Error", f"An unexpected connection error occurred: {e}")
            if self.sock: self.sock.close()
            self.destroy()

    def _receive_messages_thread(self):
        global is_spectator 
        spectator_mode_detected_local = False 

        while self.running and self.sock:
            try:
                valid, header, payload = receive_packet(self.sock) 
                if not self.running: break 

                if not valid:
                    self.server_message_queue.put(("error", "Server sent corrupted data."))
                    self.server_message_queue.put(("disconnect_event", None))
                    break
                
                if payload is None: 
                    self.server_message_queue.put(("error", "Server disconnected."))
                    self.server_message_queue.put(("disconnect_event", None))
                    break
                
                magic, seq, packet_type, data_len = header
                payload_str = payload.decode() if isinstance(payload, bytes) else payload
                
                self.server_message_queue.put(("packet", packet_type, payload_str))

                if not spectator_mode_detected_local and packet_type == PACKET_TYPE_CHAT:
                    if payload_str.strip().startswith("Welcome! You are now spectating a Battleship game."):
                        self.server_message_queue.put(("spectator_mode_on", None))
                        spectator_mode_detected_local = True
                        
            except ConnectionResetError:
                if self.running: self.server_message_queue.put(("error", "Connection to server was reset."))
                self.server_message_queue.put(("disconnect_event", None))
                break
            except BrokenPipeError:
                if self.running: self.server_message_queue.put(("error", "Connection to server was broken."))
                self.server_message_queue.put(("disconnect_event", None))
                break
            except socket.timeout: 
                if self.running: self.log_message("[DEBUG] Socket timeout in receive thread (should be handled by receive_packet).")
                continue 
            except OSError as e: 
                 if self.running: self.server_message_queue.put(("error", f"Socket error: {e}"))
                 self.server_message_queue.put(("disconnect_event", None))
                 break
            except Exception as e:
                if self.running: self.server_message_queue.put(("error", f"Error receiving from server: {e}"))
                self.server_message_queue.put(("disconnect_event", None))
                break


    def _process_gui_queue(self):
        global current_username, is_spectator

        while not self.server_message_queue.empty():
            try:
                msg_type, *data = self.server_message_queue.get_nowait()

                if msg_type == "packet":
                    packet_type, payload_str = data[0], data[1]
                    self._handle_packet(packet_type, payload_str)
                elif msg_type == "error":
                    error_msg = data[0]
                    self.log_message(f"[ERROR] {error_msg}")
                    if "username already in use" in error_msg.lower() or \
                       "expected username packet first" in error_msg.lower() or \
                       "username cannot be empty" in error_msg.lower():
                        messagebox.showerror("Connection Error", error_msg)
                        self._shutdown_client() 
                        return 
                elif msg_type == "disconnect_event":
                    self.log_message("[INFO] Disconnected from server. Saving connection info if applicable.")
                    if self.username: 
                         save_connection_info(self.username)
                    self.input_field.config(state=tk.DISABLED)
                    self.send_button.config(state=tk.DISABLED)
                    self._toggle_ship_placement_ui(show=False)
                    
                    if self.running: 
                         messagebox.showinfo("Disconnected", "Disconnected from server. You may need to restart the client.")
                    self.running = False 
                    return 
                elif msg_type == "spectator_mode_on":
                    self.is_spectator = True
                    is_spectator = True
                    self.log_message("\n[INFO] You are in spectator mode. Observe the game; no moves allowed.")
                    self.title(f"Battleship Client - {self.username} (Spectator)")
                    # Set initial labels for spectator boards
                    self.player_board_label.config(text="Player 1's Board (Spectator)")
                    if hasattr(self, 'opponent_board_name_label'): # Ensure it exists
                        self.opponent_board_name_label.config(text="Player 2's Board (Spectator)")
                    # Clear boards
                    self.draw_board_on_canvas(self.player_board_canvas, [])
                    self.draw_board_on_canvas(self.opponent_board_canvas, [])
            except Exception as e:
                self.log_message(f"[ERROR] Error processing GUI queue: {e}")

        if self.running:
            self.after(GUI_UPDATE_INTERVAL, self._process_gui_queue) 

    def _handle_packet(self, packet_type, payload_str):
        global current_username

        if packet_type == PACKET_TYPE_BOARD_UPDATE:
            self.log_message("\n" + payload_str) 
            self.update_boards_from_string(payload_str)
            
            if self.is_placing_ships and "All ships have been placed" in payload_str :
                 self._toggle_ship_placement_ui(show=False)
        elif packet_type == PACKET_TYPE_GAME_START:
            self.log_message(f"\n[GAME START] {payload_str}")
            self.player_board_label.config(text=f"Your Board ({self.username})")
        elif packet_type == PACKET_TYPE_GAME_END:
            self.log_message(f"\n[GAME END] {payload_str}")
            self._toggle_ship_placement_ui(show=False)
            if self.username: 
                try:
                    os.remove(get_connection_file(self.username))
                    self.log_message("[DEBUG] Removed connection file as game ended normally.")
                except: pass
        elif packet_type == PACKET_TYPE_ERROR:
            self.log_message(f"\n[ERROR] {payload_str}")
            if "timeout" in payload_str.lower() or "timed out" in payload_str.lower():
                 self.log_message("[ATTENTION] You have timed out! Please respond promptly.")
            if self.username and ("disconnected" in payload_str.lower() or "connection lost" in payload_str.lower() or "username already in use" in payload_str.lower()):
                save_connection_info(self.username)
                self.log_message(f"[INFO] Your username '{self.username}' was saved for potential reconnection.")
            if "Invalid placement" in payload_str:
                self.log_message("[PLACEMENT ERROR] Server rejected ship placement. Try again.")
                self.selected_coord_label.config(text="Selected Start: Invalid!")


        elif packet_type == PACKET_TYPE_RECONNECT:
            self.log_message(f"\n[RECONNECTED] {payload_str}")
            if self.username:
                mark_connection_active(self.username)
        elif packet_type == PACKET_TYPE_HEARTBEAT:
            self.log_message("[DEBUG] Received heartbeat, sending ACK")
            if self.sock: send_packet(self.sock, PACKET_TYPE_ACK, b'')
        elif packet_type == PACKET_TYPE_CHAT:
            if "Would you like to place ships manually (M) or randomly (R)?" in payload_str:
                self._prompt_manual_or_random_placement()
            elif "Place your" in payload_str and "cells)." in payload_str and self.username in payload_str:
                # E.g.,: "Player <username>, place your Carrier (5 cells)."
                self._start_manual_ship_placement(payload_str)
            elif "All ships have been placed" in payload_str:
                self.log_message(payload_str)
                if self.is_placing_ships:
                    self._toggle_ship_placement_ui(show=False)
            elif "Invalid placement. Try again" in payload_str:
                self.log_message(f"[SERVER] {payload_str}")
                # Keep placement UI open for user to retry.
                self.selected_coord_label.config(text="Selected Start: Invalid!")
            elif "already contains a ship" in payload_str:
                 self.log_message(f"[SERVER] {payload_str}")
                 self.selected_coord_label.config(text="Selected Start: Overlap!")

            is_spectator_status_msg = False
            if self.is_spectator and "Player 1:" in payload_str and "Player 2:" in payload_str and "Game State:" in payload_str:
                lines_for_names = payload_str.split('\n')
                parsed_p1 = False
                parsed_p2 = False
                for chat_line in lines_for_names:
                    clean_line = chat_line.strip()
                    if clean_line.startswith("Player 1:"):
                        try:
                            name = clean_line.split(":", 1)[1].strip()
                            if name and name != "Waiting for players":
                                self.spectator_player1_username = name
                                parsed_p1 = True
                        except IndexError:
                            pass
                    elif clean_line.startswith("Player 2:"):
                        try:
                            name = clean_line.split(":", 1)[1].strip()
                            if name and name != "Waiting for players":
                                self.spectator_player2_username = name
                                parsed_p2 = True
                        except IndexError:
                            pass
                if parsed_p1 or parsed_p2:
                    self.log_message(f"[DEBUG] Spectator names updated: P1='{self.spectator_player1_username}', P2='{self.spectator_player2_username}'")
                self.log_message(payload_str)
                is_spectator_status_msg = True

            if not is_spectator_status_msg:
                if "Spectator@" in payload_str and payload_str.startswith("[CHAT]"):
                    try:
                        parts = payload_str.split(":", 2)
                        sender_info_part = parts[0].replace("[CHAT]", "").strip()
                        message_part = parts[1].strip() if len(parts) == 2 else (parts[2].strip() if len(parts) > 2 else "")
                        if "Spectator@" in sender_info_part:
                            spectator_name = sender_info_part.split("@")[0].strip()
                            formatted_message = f"{spectator_name} (spectator): {message_part}"
                            self.log_message(f"\n{formatted_message}")
                        else:
                            self.log_message(f"\n{payload_str}")
                    except IndexError:
                         self.log_message(f"\n{payload_str}")
                else:
                    self.log_message(payload_str)
        else:
            self.log_message(f"[DEBUG] Unhandled packet type: {get_packet_type_name(packet_type)}")
            self.log_message(payload_str)
        
        self.chat_display.see(tk.END) 

    def update_boards_from_string(self, board_string):
        self.log_message("[GUI Board Update Triggered]")
        lines = board_string.strip().split('\n')

        if self.is_spectator:
            player1_grid_data = []
            player2_grid_data = []
            current_parsing_target_spectator = None

            # Update labels based on stored spectator usernames
            if self.spectator_player1_username:
                self.player_board_label.config(text=f"{self.spectator_player1_username}'s Board")
            else:
                self.player_board_label.config(text="Player 1's Board (Spectator)")

            if hasattr(self, 'opponent_board_name_label'):
                if self.spectator_player2_username:
                    self.opponent_board_name_label.config(text=f"{self.spectator_player2_username}'s Board")
                else:
                    self.opponent_board_name_label.config(text="Player 2's Board (Spectator)")

            # Define expected headers based on known usernames
            expected_p1_header = f"{self.spectator_player1_username}'s Grid:" if self.spectator_player1_username else None
            expected_p2_header = f"{self.spectator_player2_username}'s Grid:" if self.spectator_player2_username else None

            generic_p1_header_text = "Player 1's Grid:"
            generic_p2_header_text = "Player 2's Grid:"


            for line in lines:
                line_strip = line.strip()
                if not line_strip:
                    continue

                is_p1_header_match = (expected_p1_header and line_strip == expected_p1_header) or \
                                     (not expected_p1_header and line_strip == generic_p1_header_text)
                is_p2_header_match = (expected_p2_header and line_strip == expected_p2_header) or \
                                     (not expected_p2_header and line_strip == generic_p2_header_text)

                if is_p1_header_match:
                    current_parsing_target_spectator = "P1"
                    player1_grid_data = []
                    if self.spectator_player1_username:
                         self.player_board_label.config(text=f"{self.spectator_player1_username}'s Board")
                    elif line_strip == generic_p1_header_text:
                         self.player_board_label.config(text="Player 1's Board (Spectator)")
                    continue
                elif is_p2_header_match:
                    current_parsing_target_spectator = "P2"
                    player2_grid_data = []
                    if hasattr(self, 'opponent_board_name_label'):
                        if self.spectator_player2_username:
                            self.opponent_board_name_label.config(text=f"{self.spectator_player2_username}'s Board")
                        elif line_strip == generic_p2_header_text:
                            self.opponent_board_name_label.config(text="Player 2's Board (Spectator)")
                    continue

                if current_parsing_target_spectator:
                    if len(line_strip) > 1 and line_strip[0].isalpha() and line_strip[1] == ' ':
                        cells = [c for c in line_strip.split(' ') if c]
                        if cells:
                            row_char = cells.pop(0)
                            if 'A' <= row_char <= 'J' and len(row_char) == 1:
                                if len(cells) == self.board_size:
                                    if current_parsing_target_spectator == "P1":
                                        player1_grid_data.append(cells)
                                    elif current_parsing_target_spectator == "P2":
                                        player2_grid_data.append(cells)
                                else:
                                    self.log_message(f"[DEBUG SPECTATOR] Mismatched cell count for row {row_char}. Got {len(cells)}, expected {self.board_size}. Line: '{line_strip}'")
            
            if player1_grid_data or current_parsing_target_spectator == "P1":
                self.log_message(f"[DEBUG SPECTATOR] Drawing Player 1 grid ({len(player1_grid_data)} rows)")
                self.draw_board_on_canvas(self.player_board_canvas, player1_grid_data)
            if player2_grid_data or current_parsing_target_spectator == "P2":
                self.log_message(f"[DEBUG SPECTATOR] Drawing Player 2 grid ({len(player2_grid_data)} rows)")
                self.draw_board_on_canvas(self.opponent_board_canvas, player2_grid_data)

        else:
            player_grid_data = []
            opponent_grid_data = []
            current_parsing_grid = None

            for line in lines:
                line_strip = line.strip()
                if not line_strip:
                    current_parsing_grid = None
                    continue

                if "Your Grid:" in line_strip:
                    current_parsing_grid = "player"
                    self.player_board_label.config(text=f"Your Board ({self.username})")
                    continue
                elif "Opponent's Grid:" in line_strip:
                    current_parsing_grid = "opponent"
                    if hasattr(self, 'opponent_board_name_label'):
                        self.opponent_board_name_label.config(text="Opponent's Board")
                    continue
                
                # Skip column number lines (e.g., "  1 2 3 ...")
                if line_strip and line_strip[0].isspace() and any(char.isdigit() for char in line_strip):
                    if all(item.isdigit() for item in line_strip.split()):
                        continue
                
                # Parse grid data lines
                if current_parsing_grid and line_strip and line_strip[0].isalpha() and " " in line_strip : # Looks like "A . . ."
                    cells = [c for c in line_strip.split(' ') if c]
                    if cells:
                        row_char = cells.pop(0)
                        if len(cells) == self.board_size:
                            if current_parsing_grid == "player":
                                player_grid_data.append(cells)
                            elif current_parsing_grid == "opponent":
                                opponent_grid_data.append(cells)
                        else:
                            self.log_message(f"[DEBUG PLAYER] Board parse: Mismatched cell count for row {row_char}. Expected {self.board_size}, got {len(cells)}. Line: '{line_strip}'")
            
            if player_grid_data:
                self.log_message(f"[DEBUG PLAYER] Drawing player grid with data: {player_grid_data}")
                self.draw_board_on_canvas(self.player_board_canvas, player_grid_data)
            if opponent_grid_data:
                self.log_message(f"[DEBUG PLAYER] Drawing opponent grid with data: {opponent_grid_data}")
                self.draw_board_on_canvas(self.opponent_board_canvas, opponent_grid_data)


    def draw_board_on_canvas(self, canvas, grid_data):
        canvas.delete("cells")

        grid_origin_x = self.cell_size
        grid_origin_y = self.cell_size
        padding = 3 # Small padding for elements within cells
        dot_radius_factor = 0.3 # Factor of cell_size for dot radius

        # Define base cell background
        canvas_bg = canvas.cget('bg')
        water_bg_color = "#4682B4"

        for r, row_data in enumerate(grid_data):
            if r >= self.board_size: continue 
            for c, cell_char in enumerate(row_data):
                if c >= self.board_size: continue

                x0 = grid_origin_x + c * self.cell_size
                y0 = grid_origin_y + r * self.cell_size
                x1 = x0 + self.cell_size
                y1 = y0 + self.cell_size
                
                # Draw a base rectangle for the cell
                canvas.create_rectangle(x0, y0, x1, y1, fill=water_bg_color, outline='black', tags="cells")

                center_x = x0 + self.cell_size / 2
                center_y = y0 + self.cell_size / 2
                radius = self.cell_size * dot_radius_factor

                if cell_char == '.': # Water
                    pass

                elif cell_char == 'S': # Ship
                    canvas.create_rectangle(x0 + padding, y0 + padding, 
                                            x1 - padding, y1 - padding, 
                                            fill='darkgray', outline='black', width=1, tags="cells")

                elif cell_char == 'o': # Miss
                    # Draw miss on top of the water_bg_color
                    canvas.create_oval(center_x - radius, center_y - radius, 
                                        center_x + radius, center_y + radius, 
                                        fill='white', outline='white', tags="cells")

                elif cell_char == 'X': # Hit
                    canvas.create_rectangle(x0, y0, x1, y1, fill='#DC143C', outline='black', tags="cells")
                    # Draw an X on top
                    canvas.create_line(x0 + padding*2, y0 + padding*2, 
                                        x1 - padding*2, y1 - padding*2, 
                                        fill='black', width=3, tags="cells")
                    canvas.create_line(x0 + padding*2, y1 - padding*2, 
                                        x1 - padding*2, y0 + padding*2, 
                                        fill='black', width=3, tags="cells")
                
                elif cell_char == '?': # Unknown (opponent's board before reveal)
                    canvas.create_rectangle(x0, y0, x1, y1, fill='#D3D3D3', outline='black', tags="cells") # LightGray


    def _send_input(self, event=None): 
        global current_username, is_spectator 

        if not self.sock or not self.running:
            self.log_message("[ERROR] Not connected to server.")
            return

        user_input = self.input_field.get().strip()
        if not user_input:
            return

        self.input_field.delete(0, tk.END) 

        if user_input.lower() == 'quit':
            self.log_message("[INFO] Quitting the game...")
            if self.sock: send_packet(self.sock, PACKET_TYPE_DISCONNECT, "Quit requested by user")
            self._shutdown_client(save_info=False) 
            return

        packet_to_send_type = PACKET_TYPE_CHAT 
        message_to_send = user_input

        coord_match = re.fullmatch(r"([A-Ja-j])([1-9]|10)", user_input.upper())
        
        if coord_match and not self.is_placing_ships and not self.is_spectator:
            packet_to_send_type = PACKET_TYPE_MOVE
            message_to_send = user_input.upper()
            self.log_message(f"[ACTION] Sending fire coordinate from input: {message_to_send}")
        elif user_input.upper() in ["Y", "N", "YES", "NO", "M", "R"] and not self.is_spectator:
            if not self.is_placing_ships or (self.is_placing_ships and user_input.upper() in ["M", "R"]):
                packet_to_send_type = PACKET_TYPE_MOVE
                message_to_send = user_input.upper()
                self.log_message(f"[ACTION] Sending game command from input: {message_to_send}")

        if self.is_spectator and packet_to_send_type == PACKET_TYPE_MOVE:
            self.log_message("[INFO] Spectators cannot make game moves. Your message sent as chat.")
            packet_to_send_type = PACKET_TYPE_CHAT 
            message_to_send = user_input 

        if self.sock and send_packet(self.sock, packet_to_send_type, message_to_send):
            if packet_to_send_type == PACKET_TYPE_CHAT:
                display_name = self.username
                if self.is_spectator:
                    display_name += " (spectator)"
                self.log_message(f"\n{display_name}: {user_input}") 
        else:
            self.log_message("[ERROR] Failed to send message to server.")

    def log_message(self, message):
        if self.chat_display.winfo_exists():
            self.chat_display.config(state=tk.NORMAL)
            self.chat_display.insert(tk.END, message + "\n")
            self.chat_display.config(state=tk.DISABLED)
            self.chat_display.see(tk.END)
        else:
            print(f"[LOG (Chat Display N/A)]: {message}")


    def _on_closing(self):
        if messagebox.askokcancel("Quit", "Do you want to quit Battleship?"):
            self.log_message("[INFO] Quit by closing window.")
            if self.sock:
                 send_packet(self.sock, PACKET_TYPE_DISCONNECT, "Client closed window")
            self._shutdown_client(save_info=True) 

    def _shutdown_client(self, save_info=True):
        self.running = False 
        if self.username and save_info:
            save_connection_info(self.username) 
        
        if self.network_thread and self.network_thread.is_alive():
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR) 
                except OSError: pass 
                try:
                    self.sock.close()
                except OSError: pass

        if self.winfo_exists(): # Check if window still exists before destroying
            self.destroy()

# Dialog for Reconnection
class ReconnectionDialog(simpledialog.Dialog):
    def __init__(self, parent, title, options):
        self.options = options
        self.choice = None 
        super().__init__(parent, title)

    def body(self, master):
        tk.Label(master, text="Recent disconnection(s) found:").pack(pady=5)
        self.listbox = tk.Listbox(master, selectmode=tk.SINGLE, exportselection=False)
        for i, option_text in enumerate(self.options):
            self.listbox.insert(tk.END, f"{i+1}. {option_text}")
        self.listbox.pack(padx=10, pady=5)
        self.listbox.bind("<Double-Button-1>", self.ok) 
        return self.listbox 

    def buttonbox(self):
        box = tk.Frame(self)
        tk.Button(box, text="Reconnect Selected", width=20, command=self.ok, default=tk.ACTIVE).pack(side=tk.LEFT, padx=5, pady=5)
        tk.Button(box, text="New Connection", width=15, command=self.new_connection).pack(side=tk.LEFT, padx=5, pady=5)
        tk.Button(box, text="Cancel", width=10, command=self.cancel).pack(side=tk.LEFT, padx=5, pady=5)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)
        box.pack()

    def ok(self, event=None):
        selection = self.listbox.curselection()
        if selection:
            self.choice = selection[0]
        else:
            self.choice = None 
        super().ok()
    
    def new_connection(self):
        self.choice = -1 
        super().ok() 

    def cancel(self):
        self.choice = None 
        super().cancel()


if __name__ == "__main__":
    app = BattleshipGUI()
    try:
        app.mainloop()
    except KeyboardInterrupt:
        if hasattr(app, 'running') and app.running : 
             if hasattr(app, 'log_message'): app.log_message("[INFO] Client exiting due to keyboard interrupt.")
             if hasattr(app, '_shutdown_client'): app._shutdown_client(save_info=True)
    finally:
        pass