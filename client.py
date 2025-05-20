"""
client.py

This module implements a GUI client for the Battleship game, handling network communication,
game state management, and UI interactions. It supports ship placement,
gameplay, chat functionality, and reconnection.
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

# Network configuration
HOST = '127.0.0.1'
PORT = 5001
GUI_UPDATE_INTERVAL = 100

# Standard ship definitions
SHIPS = [
    ("Carrier", 5),
    ("Battleship", 4),
    ("Cruiser", 3),
    ("Submarine", 3),
    ("Destroyer", 2)
]

# Global state
current_username = ""
is_spectator = False

# Reconnection data management
project_root = os.getcwd() 
battleship_dir = os.path.join(project_root, ".reconnection_data")
os.makedirs(battleship_dir, exist_ok=True)

def get_connection_file(username):
    """Returns the path to the connection file for a given username."""
    if not username:
        return None
    return os.path.join(battleship_dir, f".battleship_connection_{username}.json")

def save_connection_info(username):
    """Saves connection information for a username when disconnected."""
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
        pass

def mark_connection_active(username):
    """Marks a connection as active by updating its timestamp and disconnected status."""
    if not username:
        return
    connection_file = get_connection_file(username)
    try:
        if not os.path.exists(connection_file):
            with open(connection_file, 'w') as f:
                json.dump({'username': username, 'timestamp': time.time(), 'disconnected': False}, f)
        else:
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

def load_connection_info(username):
    """Loads connection information for a username and checks if reconnection is possible."""
    if not username:
        return False
    connection_file = get_connection_file(username)
    try:
        if os.path.exists(connection_file):
            with open(connection_file, 'r') as f:
                data = json.load(f)
                elapsed = time.time() - data.get('timestamp', 0)
                if elapsed <= 60 and data.get('disconnected', False):
                    return True
                elif elapsed > 60:
                    try:
                        os.remove(connection_file)
                    except:
                        pass
    except Exception as e:
        pass
    return False

def check_any_recent_connections():
    """Checks for any recent disconnections that can be reconnected to."""
    recent_usernames = []
    stale_files_to_remove = []
    current_time = time.time()
    reconnect_timeout_period = 60

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
                        elapsed = current_time - timestamp

                        if elapsed > reconnect_timeout_period:
                            stale_files_to_remove.append(connection_file)
                            continue

                        if username and disconnected:
                            recent_usernames.append((username, elapsed))
                except json.JSONDecodeError:
                    stale_files_to_remove.append(connection_file)
                except Exception as e_read:
                    continue
    except Exception as e_list:
        pass

    for file_to_remove in stale_files_to_remove:
        try:
            os.remove(file_to_remove)
        except Exception as e_remove:
            pass

    recent_usernames.sort(key=lambda x: x[1])
    return recent_usernames

# Message type configuration for chat display
MSG_TYPE_CONFIG = {
    "self_chat": {
        "split_message": True,
        "sender_tag": "self_msg_sender",
        "text_tag": "self_msg_text",
    },
    "other_chat": {
        "split_message": True,
        "sender_tag": "other_msg_sender",
        "text_tag": "other_msg_text",
    },
    "spectator_chat": {
        "split_message": True,
        "sender_tag": "spectator_msg_sender",
        "text_tag": "spectator_msg_text",
    },
    "server_chat": {
        "sender_name_override": "Server",
        "sender_tag": "server_info_sender",
        "text_tag": "server_info_text",
    },
    "error": {
        "sender_name_override": "System",
        "sender_tag": "error_msg_sender",
        "text_tag": "error_msg_text",
    },
    "info": {
        "sender_name_override": "System",
        "sender_tag": "server_info_sender",
        "text_tag": "server_info_text",
    },
    "game_event": {"text_tag": "game_event_text"},
    "action_log": {"text_tag": "action_log_text"},
    "placement_log": {"text_tag": "placement_log_text"},
    "debug": {"text_tag": "debug_log_text"},
    None: {"text_tag": "other_msg_text"} 
}

class BattleshipGUI(tk.Tk):
    """Main GUI class for the Battleship game client."""
    
    def __init__(self):
        super().__init__()
        self.title("Battleship Client")
        self.geometry("1100x750")

        # Network and game state
        self.sock = None
        self.server_message_queue = Queue()
        self.network_thread = None
        self.username = ""
        self.is_spectator = False
        self.running = True

        # Spectator state
        self.spectator_player1_username = None
        self.spectator_player2_username = None

        # Game state
        self.last_fired_coord = None
        self.awaiting_shot_result = False
        self.sunk_ships_on_my_board_coords = []
        self.sunk_ships_on_opponent_board_coords = []
        self.opponent_sunk_ship_names = set()

        # Board configuration
        self.board_size = 10
        self.cell_size = 30

        # Ship placement state
        self.is_placing_ships = False
        self.ships_to_place_list = []
        self.current_ship_to_place_idx = 0
        self.current_ship_name = ""
        self.current_ship_length = 0
        self.selected_placement_coord = None
        self.placement_orientation_var = tk.StringVar(value="H")

        self._setup_ui()
        self._prompt_for_username_and_connect()
        
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _setup_ui(self):
        """Initializes the main UI components including game boards and chat area."""
        main_frame = tk.Frame(self)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Create window for game area and side panels
        self.paned_window = tk.PanedWindow(main_frame, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=5)
        self.paned_window.pack(fill=tk.BOTH, expand=True)

        # Game area frame
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
        
        # Opponent Progress UI
        self.opponent_progress_frame = tk.LabelFrame(self.game_area_frame, text="Opponent's Fleet Status", pady=5)

        self.opponent_ship_status_labels = {}
        for ship_name, ship_length in SHIPS:
            label_text = f"{ship_name} ({ship_length}): Active"
            label = tk.Label(self.opponent_progress_frame, text=label_text)
            label.pack(anchor="w")
            self.opponent_ship_status_labels[ship_name] = label

        # Ship Placement UI
        self.placement_frame = tk.Frame(self.game_area_frame, pady=10)
        self.placement_prompt_label = tk.Label(self.placement_frame, text="Ship Placement Options:")
        self.placement_prompt_label.pack()

        self.manual_random_frame = tk.Frame(self.placement_frame)
        self.place_manual_button = tk.Button(self.manual_random_frame, text="Place Manually", command=lambda: self._send_placement_choice("M"))
        self.place_manual_button.pack(side=tk.LEFT, padx=5)
        self.place_random_button = tk.Button(self.manual_random_frame, text="Place Randomly", command=lambda: self._send_placement_choice("R"))
        self.place_random_button.pack(side=tk.LEFT, padx=5)

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

        # Draw grid lines
        self.draw_grid_lines(self.player_board_canvas)
        self.draw_grid_lines(self.opponent_board_canvas)
        
        # Create side panels frame
        side_panels_frame = tk.Frame(self.paned_window)
        self.paned_window.add(side_panels_frame, minsize=300)

        # Chat panel
        chat_frame = tk.LabelFrame(side_panels_frame, text="Chat", relief=tk.SUNKEN, borderwidth=1)
        chat_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.chat_display = scrolledtext.ScrolledText(chat_frame, height=10, state=tk.DISABLED, wrap=tk.WORD)
        self.chat_display.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Chat input
        chat_input_frame = tk.Frame(chat_frame)
        chat_input_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)
        self.chat_input = tk.Entry(chat_input_frame)
        self.chat_input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.chat_input.bind("<Return>", lambda e: self._send_chat())
        self.chat_send_button = tk.Button(chat_input_frame, text="Send", command=self._send_chat)
        self.chat_send_button.pack(side=tk.RIGHT)

        # System Info panel
        system_info_frame = tk.LabelFrame(side_panels_frame, text="System Info", relief=tk.SUNKEN, borderwidth=1)
        system_info_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.system_info_display = scrolledtext.ScrolledText(system_info_frame, height=10, state=tk.DISABLED, wrap=tk.WORD)
        self.system_info_display.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=5, pady=5)

        # System Info input
        system_info_input_frame = tk.Frame(system_info_frame)
        system_info_input_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)
        self.system_info_input = tk.Entry(system_info_input_frame)
        self.system_info_input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.system_info_input.bind("<Return>", self._send_command)
        self.system_info_send_button = tk.Button(system_info_input_frame, text="Send", command=self._send_command)
        self.system_info_send_button.pack(side=tk.RIGHT)

        # Message styling configuration
        self._configure_chat_tags()
        self._configure_command_tags()

    def _configure_chat_tags(self):
        """Configures text styling for different types of chat messages."""
        self.chat_display.tag_configure("timestamp", foreground="#888888", font=("Helvetica", 8))
        self.chat_display.tag_configure("self_msg_sender", foreground="blue", font=("Helvetica", 9, "bold"))
        self.chat_display.tag_configure("self_msg_text", foreground="blue")
        self.chat_display.tag_configure("other_msg_sender", foreground="green", font=("Helvetica", 9, "bold"))
        self.chat_display.tag_configure("other_msg_text", foreground="black")
        self.chat_display.tag_configure("spectator_msg_sender", foreground="purple", font=("Helvetica", 9, "bold"))
        self.chat_display.tag_configure("spectator_msg_text", foreground="purple")
        self.chat_display.tag_configure("server_info_sender", foreground="royalblue", font=("Helvetica", 9, "bold"))
        self.chat_display.tag_configure("server_info_text", foreground="royalblue")
        self.chat_display.tag_configure("error_msg_sender", foreground="red", font=("Helvetica", 9, "bold"))
        self.chat_display.tag_configure("error_msg_text", foreground="red")
        self.chat_display.tag_configure("game_event_text", foreground="#555555", font=("Helvetica", 9, "italic"))
        self.chat_display.tag_configure("action_log_text", foreground="darkcyan", font=("Helvetica", 9))
        self.chat_display.tag_configure("placement_log_text", foreground="magenta", font=("Helvetica", 9))
        self.chat_display.tag_configure("debug_log_text", foreground="gray", font=("Helvetica", 8, "italic"))

    def _configure_command_tags(self):
        """Configures text styling for different types of command messages."""
        self.system_info_display.tag_configure("timestamp", foreground="#888888", font=("Helvetica", 8))
        self.system_info_display.tag_configure("game_event_text", foreground="#555555", font=("Helvetica", 9, "italic"))
        self.system_info_display.tag_configure("action_log_text", foreground="darkcyan", font=("Helvetica", 9))
        self.system_info_display.tag_configure("placement_log_text", foreground="magenta", font=("Helvetica", 9))
        self.system_info_display.tag_configure("error_msg_sender", foreground="red", font=("Helvetica", 9, "bold"))
        self.system_info_display.tag_configure("error_msg_text", foreground="red")
        self.system_info_display.tag_configure("server_info_sender", foreground="royalblue", font=("Helvetica", 9, "bold"))
        self.system_info_display.tag_configure("server_info_text", foreground="royalblue")
        self.system_info_display.tag_configure("debug_log_text", foreground="gray", font=("Helvetica", 8, "italic"))

    def _toggle_ship_placement_ui(self, show=False, show_mr_choice=False):
        """Toggles the visibility of ship placement UI elements."""
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
                self.opponent_progress_frame.pack_forget()
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
                self.opponent_progress_frame.pack_forget()
        else:
            self.placement_frame.pack_forget()
            if not self.is_spectator:
                self.opponent_progress_frame.pack(side=tk.TOP, fill=tk.X, pady=5, after=self.boards_frame)

        self.is_placing_ships = show and not show_mr_choice

    def draw_grid_lines(self, canvas):
        """Draws the grid lines and labels for a game board."""
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
        
        canvas.config(width=self.cell_size * (self.board_size + 1), height=self.cell_size * (self.board_size + 1))

    def _canvas_coord_to_grid_coord(self, event_x, event_y):
        """Converts canvas coordinates to grid coordinates."""
        grid_origin_x = self.cell_size
        grid_origin_y = self.cell_size
        
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
        """Handles clicks on the opponent's board during gameplay."""
        if self.is_spectator or self.is_placing_ships or self.awaiting_shot_result:
            return
        
        self.awaiting_shot_result = True

        coord = self._canvas_coord_to_grid_coord(event.x, event.y)
        if coord and self.sock:
            if send_packet(self.sock, PACKET_TYPE_MOVE, coord):
                self.last_fired_coord = coord
            else:
                self.awaiting_shot_result = False
                self.log_command("[ERROR] Failed to send fire command.", msg_type="error")

    def _on_player_board_click(self, event):
        """Handles clicks on the player's board during ship placement."""
        if not self.is_placing_ships or self.is_spectator:
            return

        coord = self._canvas_coord_to_grid_coord(event.x, event.y)
        if coord:
            self.selected_placement_coord = coord
            self.selected_coord_label.config(text=f"Selected Start: {coord}")

    def _prompt_manual_or_random_placement(self):
        """Prompts the user to choose between manual or random ship placement."""
        self.log_command("[SERVER] Would you like to place ships manually (M) or randomly (R)?", msg_type="server_info")
        self._toggle_ship_placement_ui(show=True, show_mr_choice=True)

    def _send_placement_choice(self, choice):
        """Sends the user's ship placement choice to the server using the system info command logic."""
        if self.sock:
            send_packet(self.sock, PACKET_TYPE_MOVE, choice)
            self.log_command(f"[ACTION] Sent placement choice: {choice}", msg_type="action_log")
            self._toggle_ship_placement_ui(show=False)
            if choice.upper() == "M":
                 self.log_command("[INFO] Waiting for server to send ship details for manual placement...", msg_type="info")

    def _start_manual_ship_placement(self, ships_string_from_server):
        """Starts the manual ship placement process with the given ship details."""
        match = re.search(r"Placing your ([A-Za-z\s]+)\s*\(size (\d+)\)", ships_string_from_server)
        if match:
            self.current_ship_name = match.group(1).strip()
            self.current_ship_length = int(match.group(2))
            self.log_command(f"[PLACEMENT] Now placing: {self.current_ship_name} (Length: {self.current_ship_length})", msg_type="info")
            self.current_ship_label.config(text=f"Placing: {self.current_ship_name} ({self.current_ship_length} cells)")
            self.selected_coord_label.config(text="Selected Start: None")
            self.selected_placement_coord = None
            self._toggle_ship_placement_ui(show=True, show_mr_choice=False)
        else:
            self.log_command(f"[ERROR] Could not parse ship details from server: {ships_string_from_server}", msg_type="error")
            self._toggle_ship_placement_ui(show=False)

    def _confirm_ship_placement_action(self):
        """Confirms and sends the current ship placement to the server."""
        if not self.selected_placement_coord:
            messagebox.showwarning("Placement Error", "Please select a starting cell on your board.", parent=self)
            return
        if not self.current_ship_name:
            messagebox.showerror("Placement Error", "No current ship to place. Waiting for server.", parent=self)
            return

        orientation = self.placement_orientation_var.get()
        placement_command = f"{self.selected_placement_coord} {orientation}"
        
        self.log_command(f"[ACTION] Sending placement for {self.current_ship_name}: {placement_command}", msg_type="action_log")
        if self.sock:
            send_packet(self.sock, PACKET_TYPE_MOVE, placement_command)
            
        self.selected_coord_label.config(text="Selected Start: Waiting...")

    def _prompt_for_username_and_connect(self):
        """Handles the initial username prompt and connection process."""
        global current_username 

        chosen_username = simpledialog.askstring("Username", "Enter your username:", parent=self)

        if not chosen_username:
            self.log_command("[ERROR] Username cannot be empty. Exiting.", msg_type="error")
            messagebox.showerror("Error", "Username cannot be empty. The application will now close.")
            self.destroy()
            return

        self.username = chosen_username
        current_username = self.username 

        self._try_connect_with_username(self.username)

    def _try_connect_with_username(self, username):
        """Establishes a connection to the server with the given username."""
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

        if self.network_thread and self.network_thread.is_alive():
            self.running = False
            time.sleep(0.2)
            self.running = True

        try:
            # Create a brand new socket
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((HOST, PORT))

            if send_packet(self.sock, PACKET_TYPE_USERNAME, username):
                save_connection_info(username) 
                mark_connection_active(username) 
                
                # Start a new network thread
                self.network_thread = threading.Thread(target=self._receive_messages_thread, daemon=True)
                self.network_thread.start()
                
                self.after(GUI_UPDATE_INTERVAL, self._process_gui_queue)
                
                self.log_command("[INFO] You may be placed as a player or spectator.", msg_type="info")
                self.log_command("[INFO] Type messages in the input field below and press Enter or Send Chat.", msg_type="info")
                self.log_command("[INFO] Click on opponent's board to fire. Follow prompts for ship placement.", msg_type="info")
            else:
                self.log_command("[ERROR] Failed to send username to server.", msg_type="error")
                messagebox.showerror("Connection Error", "Failed to send username to server.")
                if self.sock: self.sock.close()
                self.sock = None
                self.destroy()
        except ConnectionRefusedError:
            self.log_command(f"[ERROR] Could not connect to server at {HOST}:{PORT}. Check if server is running.", msg_type="error")
            messagebox.showerror("Connection Error", f"Could not connect to server at {HOST}:{PORT}.\nCheck if the server is running.")
            self.destroy()
        except Exception as e:
            self.log_command(f"[ERROR] Connection error: {e}", msg_type="error")
            messagebox.showerror("Connection Error", f"An unexpected connection error occurred: {e}")
            if self.sock: self.sock.close()
            self.sock = None
            self.destroy()

    def _receive_messages_thread(self):
        """Background thread for receiving and processing server messages."""
        global is_spectator 
        spectator_mode_detected_local = False 

        while self.running and self.sock:
            try:
                valid, header, payload = receive_packet(self.sock) 
                if not self.running: break 

                if not valid:
                    if header is None:
                        self.server_message_queue.put(("disconnect_event", None))
                        break
                    else:
                        self.server_message_queue.put(("error", "Server sent corrupted data."))
                        self.server_message_queue.put(("disconnect_event", None))
                        break
                
                if payload is None: 
                    self.server_message_queue.put(("disconnect_event", None))
                    break
                
                magic, seq, packet_type, data_len = header
                payload_str = payload.decode() if isinstance(payload, bytes) else payload
                
                if packet_type == PACKET_TYPE_ERROR:
                    if "username already in use" in payload_str.lower():
                        self.server_message_queue.put(("error", payload_str))
                        break
                    else:
                        self.server_message_queue.put(("error", payload_str))
                        
                        if "timeout" in payload_str.lower() or "timed out" in payload_str.lower():
                            self.log_command("[ATTENTION] You have timed out! Please respond promptly.", msg_type="error")
                        elif "Invalid placement" in payload_str:
                            self.log_command("[PLACEMENT ERROR] Server rejected ship placement. Try again.", msg_type="placement_log")
                            if hasattr(self, 'selected_coord_label'):
                                self.selected_coord_label.config(text="Selected Start: Invalid!")
                    
                    continue
                
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
        """Processes messages from the server message queue and updates the GUI accordingly."""
        global current_username, is_spectator

        while not self.server_message_queue.empty():
            try:
                msg_type, *data = self.server_message_queue.get_nowait()

                if msg_type == "packet":
                    packet_type, payload_str = data[0], data[1]
                    self._handle_packet(packet_type, payload_str)
                elif msg_type == "username_error":
                    error_msg = data[0]
                    self.log_command(f"[ERROR] {error_msg}", msg_type="error")
                    self.log_command("[INFO] Please enter a different username", msg_type="info")
                    
                    if self.sock:
                        try:
                            self.sock.close()
                        except:
                            pass
                        self.sock = None
                    
                    new_username = simpledialog.askstring("Username", "Username already in use. Please enter a different username:", parent=self)
                    if new_username:
                        self.username = new_username
                        current_username = new_username
                        self._try_connect_with_username(new_username)
                    else:
                        self._shutdown_client(save_info=False)
                    return
                elif msg_type == "error":
                    error_msg = data[0]
                    self.log_command(f"[ERROR] {error_msg}", msg_type="error")
                    
                    if "username already in use" in error_msg.lower():
                        if self.sock:
                            try:
                                self.sock.close()
                            except:
                                pass
                            self.sock = None
                        
                        new_username = simpledialog.askstring("Username", "Username already in use. Please enter a different username:", parent=self)
                        if new_username:
                            self.username = new_username
                            current_username = new_username
                            self._try_connect_with_username(new_username)
                        else:
                            self._shutdown_client(save_info=False)
                        return
                    
                    elif "disconnected" in error_msg.lower() or "connection lost" in error_msg.lower():
                        if self.username:
                            save_connection_info(self.username)
                            self.log_command(f"[INFO] Your username '{self.username}' was saved for potential reconnection.", msg_type="info")
                            
                    elif "expected username packet first" in error_msg.lower() or \
                       "username cannot be empty" in error_msg.lower():
                        messagebox.showerror("Connection Error", error_msg)
                        self._shutdown_client() 
                        return 
                elif msg_type == "disconnect_event":
                    self.log_command("[INFO] Disconnected from server. Saving connection info.", msg_type="info")
                    if self.username: 
                         save_connection_info(self.username)
                    self.chat_input.config(state=tk.DISABLED)
                    self.chat_send_button.config(state=tk.DISABLED)
                    self._toggle_ship_placement_ui(show=False)
                    
                    if self.running: 
                         messagebox.showinfo("Disconnected", "Disconnected from server. You may need to restart the client.")
                    self.running = False 
                    return 
                elif msg_type == "spectator_mode_on":
                    self.is_spectator = True
                    is_spectator = True
                    self.log_command("\n[INFO] You are in spectator mode. Observe the game - no moves allowed.", msg_type="info")
                    self.title(f"Battleship Client - {self.username} (Spectator)")
                    self.player_board_label.config(text="Player 1's Board (Spectator)")
                    if hasattr(self, 'opponent_board_name_label'): 
                        self.opponent_board_name_label.config(text="Player 2's Board (Spectator)")
                    self.draw_board_on_canvas(self.player_board_canvas, [])
                    self.draw_board_on_canvas(self.opponent_board_canvas, [])
            except Exception as e:
                self.log_command(f"[ERROR] Error processing GUI queue: {e}")

        if self.running:
            self.after(GUI_UPDATE_INTERVAL, self._process_gui_queue)

    def _handle_packet(self, packet_type, payload_str):
        """Handles different types of packets received from the server."""
        global current_username

        if packet_type == PACKET_TYPE_CHAT:
            is_game_flow_message = False
            if "Would you like to place ships manually (M) or randomly (R)?" in payload_str:
                self._prompt_manual_or_random_placement()
                self.log_command(payload_str, msg_type="info")
                is_game_flow_message = True
                return
            elif payload_str.startswith("Placing your ") and "(size " in payload_str and payload_str.endswith(")."):
                self._start_manual_ship_placement(payload_str)
                self.log_command(payload_str, msg_type="placement_log")
                is_game_flow_message = True
                return
            elif "All ships have been placed" in payload_str:
                self.log_command(payload_str, msg_type="game_event")
                if self.is_placing_ships:
                    self._toggle_ship_placement_ui(show=False)
                if not self.is_spectator:
                    self.opponent_progress_frame.pack(side=tk.TOP, fill=tk.X, pady=5, after=self.boards_frame)
                is_game_flow_message = True

            elif "Invalid placement. Try again" in payload_str:
                self.log_command(f"[SERVER] {payload_str}", msg_type="placement_log")
                if hasattr(self, 'selected_coord_label'):
                    self.selected_coord_label.config(text="Selected Start: Invalid!")
                is_game_flow_message = True

            elif "already contains a ship" in payload_str:
                self.log_command(f"[SERVER] {payload_str}", msg_type="placement_log")
                if hasattr(self, 'selected_coord_label'):
                    self.selected_coord_label.config(text="Selected Start: Overlap!")
                is_game_flow_message = True

            if not is_game_flow_message and payload_str.startswith("[CHAT]"):
                try:
                    parts = payload_str.split(":", 2)
                    sender_info_part = parts[0].replace("[CHAT]", "").strip()
                    message_part = parts[2].strip() if len(parts) > 2 else (parts[1].strip() if len(parts) > 1 else "")
                    
                    if "Spectator@" in sender_info_part:
                        spectator_name = sender_info_part.split("@", 1)[1].strip()
                        self.log_message(f"{spectator_name} (spectator): {message_part}", msg_type="spectator_chat")
                    else:
                        player_name = sender_info_part if sender_info_part else self.username
                        if player_name == self.username:
                            pass
                        else:
                            self.log_message(f"{player_name}: {message_part}", msg_type="other_chat")
                except Exception as e:
                    self.log_command(f"Error parsing chat: {payload_str} - {e}", msg_type="error")
                return

            elif not is_game_flow_message:
                self.log_command(payload_str, msg_type="info")
                return

        elif packet_type == PACKET_TYPE_BOARD_UPDATE:
            if self.awaiting_shot_result and self.last_fired_coord:
                lines = payload_str.strip().split('\n')
                opponent_grid_start = -1
                for i, line in enumerate(lines):
                    if "Opponent's Grid:" in line:
                        opponent_grid_start = i
                        break
                
                if opponent_grid_start != -1:
                    row = ord(self.last_fired_coord[0].upper()) - ord('A')
                    col = int(self.last_fired_coord[1:]) - 1
                    
                    for i in range(opponent_grid_start + 1, len(lines)):
                        line_content = lines[i].strip()
                        if not line_content:
                            break
                        if line_content and line_content[0].isalpha() and len(line_content) > 1 and line_content[1] == ' ':
                            cells = line_content.split()[1:]
                            if len(cells) == self.board_size:
                                if i - (opponent_grid_start + 1) == row:
                                    result = cells[col]
                                    if result == 'X':
                                        self.log_command(f"[SHOT RESULT] Hit at {self.last_fired_coord}!", msg_type="action_log")
                                    elif result == 'o':
                                        self.log_command(f"[SHOT RESULT] Miss at {self.last_fired_coord}!", msg_type="action_log")
                                    break

            event_summary = self._filter_board_data_for_logging(payload_str)
            if event_summary:
                self.log_command("\n" + event_summary, msg_type="game_event")
            self.update_boards_from_string(payload_str)
            self.awaiting_shot_result = False
            self.last_fired_coord = None
        
        elif packet_type == PACKET_TYPE_GAME_START:
            self.log_command("\n[GAME START] " + payload_str, msg_type="game_event")
            self._prompt_manual_or_random_placement()
            return 

        elif packet_type == PACKET_TYPE_GAME_END:
            self.log_command("\n[GAME END] " + payload_str, msg_type="game_event")
            self._toggle_ship_placement_ui(show=False)
            if self.username: 
                try:
                    os.remove(get_connection_file(self.username))
                except: pass

        elif packet_type == PACKET_TYPE_ERROR:
            self.log_command(f"[SERVER ERROR] {payload_str}", msg_type="error")

        elif packet_type == PACKET_TYPE_RECONNECT:
            self.log_command("\n[RECONNECTED] " + payload_str, msg_type="info")
            if self.username:
                mark_connection_active(self.username)

        elif packet_type == PACKET_TYPE_HEARTBEAT:
            if self.sock: send_packet(self.sock, PACKET_TYPE_ACK, b'')

        else:

            self.log_command(f"[UNHANDLED PACKET TYPE {get_packet_type_name(packet_type)}] {payload_str}", msg_type="debug")

        # Ensure displays are scrolled to the end
        self.chat_display.see(tk.END)
        self.system_info_display.see(tk.END)

    def _filter_board_data_for_logging(self, board_update_payload):
        """Filters board update data to extract relevant event information for logging."""
        event_lines = []
        lines = board_update_payload.strip().split('\n')

        for line in lines:
            stripped_line = line.strip()

            is_grid_header = False
            if stripped_line.endswith(" Grid:"):
                if stripped_line == "Your Grid:" or \
                   stripped_line == "Opponent's Grid:" or \
                   stripped_line == "Player 1's Grid:" or \
                   stripped_line == "Player 2's Grid:":
                    is_grid_header = True
                elif self.spectator_player1_username and stripped_line == f"{self.spectator_player1_username}'s Grid:":
                    is_grid_header = True
                elif self.spectator_player2_username and stripped_line == f"{self.spectator_player2_username}'s Grid:":
                    is_grid_header = True
            
            if is_grid_header:
                continue

            items_in_line = stripped_line.split()
            if len(items_in_line) > 1 and all(item.isdigit() for item in items_in_line):
                is_likely_column_header = True
                if len(items_in_line) == self.board_size:
                    try:
                        for i, item_val in enumerate(items_in_line):
                            if int(item_val) != i + 1:
                                is_likely_column_header = False
                                break
                    except ValueError:
                        is_likely_column_header = False
                else:
                    is_likely_column_header = False

                if is_likely_column_header:
                    continue
            
            if (len(stripped_line) > 1 and 
                'A' <= stripped_line[0].upper() <= chr(ord('A') + self.board_size - 1) and 
                stripped_line[1] == ' ' and
                len(stripped_line.split()) == self.board_size + 1):
                continue
            
            event_lines.append(line)
        
        result = "\n".join(event_lines).strip() 
        return result

    def update_boards_from_string(self, board_string):
        """Updates the game boards based on the received board string from the server."""
        lines = board_string.strip().split('\n')

        self.sunk_ships_on_my_board_coords = []
        self.sunk_ships_on_opponent_board_coords = []
        current_sunk_list_target = None

        if self.is_spectator:
            player1_grid_data = []
            player2_grid_data = []
            current_parsing_target_spectator = None

            if self.spectator_player1_username:
                self.player_board_label.config(text=f"{self.spectator_player1_username}'s Board")
            else:
                self.player_board_label.config(text="Player 1's Board (Spectator)")

            if hasattr(self, 'opponent_board_name_label'):
                if self.spectator_player2_username:
                    self.opponent_board_name_label.config(text=f"{self.spectator_player2_username}'s Board")
                else:
                    self.opponent_board_name_label.config(text="Player 2's Board (Spectator)")

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
                                    pass
            
            if player1_grid_data or current_parsing_target_spectator == "P1":
                self.draw_board_on_canvas(self.player_board_canvas, player1_grid_data)
            if player2_grid_data or current_parsing_target_spectator == "P2":
                self.draw_board_on_canvas(self.opponent_board_canvas, player2_grid_data)

            if current_parsing_target_spectator == "P1":
                current_sunk_list_target = self.sunk_ships_on_my_board_coords
            elif current_parsing_target_spectator == "P2":
                current_sunk_list_target = self.sunk_ships_on_opponent_board_coords

        else:
            current_parsing_grid = None
            current_grid_data = []

            for line in lines:
                line_strip = line.strip()
                if not line_strip:
                    if current_parsing_grid == "player" and current_grid_data:
                        self.draw_board_on_canvas(self.player_board_canvas, current_grid_data)
                        current_grid_data = []
                    elif current_parsing_grid == "opponent" and current_grid_data:
                        self.draw_board_on_canvas(self.opponent_board_canvas, current_grid_data)
                        current_grid_data = []
                    continue

                if "Your Grid:" in line_strip:
                    current_parsing_grid = "player"
                    self.player_board_label.config(text=f"Your Board ({self.username})")
                    current_grid_data = []
                    current_sunk_list_target = self.sunk_ships_on_my_board_coords
                    self.opponent_sunk_ship_names.clear()
                    continue
                elif "Opponent's Grid:" in line_strip:
                    current_parsing_grid = "opponent"
                    if hasattr(self, 'opponent_board_name_label'):
                        self.opponent_board_name_label.config(text="Opponent's Board")
                    current_grid_data = []
                    current_sunk_list_target = self.sunk_ships_on_opponent_board_coords
                    continue
                
                sunk_info_key = "SUNK_SHIPS_INFO:"
                if line_strip.startswith(sunk_info_key):
                    data_part = line_strip[len(sunk_info_key):]
                    if data_part:
                        ship_entries = data_part.split(';')
                        for entry in ship_entries:
                            if ':' not in entry: continue
                            name_part, coords_data_part = entry.split(':', 1)
                            if current_parsing_grid == "opponent" or (self.is_spectator and current_parsing_target_spectator == "P2"):
                                self.opponent_sunk_ship_names.add(name_part)
                            
                            if current_sunk_list_target is not None:
                                coord_pair_strs = coords_data_part.split('_')
                                ship_cell_coords = set()
                                for cp_str in coord_pair_strs:
                                    if ',' not in cp_str: continue
                                    try:
                                        r_str, c_str = cp_str.split(',')
                                        r, c = int(r_str), int(c_str)
                                        if 0 <= r < self.board_size and 0 <= c < self.board_size:
                                            ship_cell_coords.add((r, c))
                                    except ValueError:
                                        self.log_command(f"[DEBUG] Failed to parse sunk ship r,c pair: {cp_str}", "debug")
                                if ship_cell_coords:
                                    current_sunk_list_target.append(ship_cell_coords)
                    continue

                if line_strip and line_strip[0].isspace() and any(char.isdigit() for char in line_strip):
                    if all(item.isdigit() for item in line_strip.split()):
                        continue
                
                if current_parsing_grid and line_strip and line_strip[0].isalpha() and " " in line_strip:
                    cells = [c for c in line_strip.split(' ') if c]
                    if cells:
                        row_char = cells.pop(0)
                        if len(cells) == self.board_size:
                            current_grid_data.append(cells)

            if self.is_spectator:
                if current_parsing_target_spectator == "P1":
                    current_sunk_list_target = self.sunk_ships_on_my_board_coords
                elif current_parsing_target_spectator == "P2":
                    current_sunk_list_target = self.sunk_ships_on_opponent_board_coords
            else:
                if current_parsing_grid == "player":
                    current_sunk_list_target = self.sunk_ships_on_my_board_coords
                elif current_parsing_grid == "opponent":
                    current_sunk_list_target = self.sunk_ships_on_opponent_board_coords

            if not self.is_spectator:
                if current_parsing_grid == "player" and current_grid_data:
                    self.draw_board_on_canvas(self.player_board_canvas, current_grid_data)
                elif current_parsing_grid == "opponent" and current_grid_data:
                    self.draw_board_on_canvas(self.opponent_board_canvas, current_grid_data)

        if not self.is_spectator:
            self._update_opponent_progress_ui()

    def _update_opponent_progress_ui(self):
        """Updates the labels in the opponent progress frame based on sunk ship names."""
        if not hasattr(self, 'opponent_ship_status_labels'):
            return

        for ship_name, ship_length in SHIPS:
            status_label = self.opponent_ship_status_labels.get(ship_name)
            if status_label:
                if ship_name in self.opponent_sunk_ship_names:
                    status_label.config(text=f"{ship_name} ({ship_length}): SUNK!", fg="red")
                else:
                    status_label.config(text=f"{ship_name} ({ship_length}): Active", fg="green")

    def draw_board_on_canvas(self, canvas, grid_data):
        """Draws the game board on the specified canvas using the provided grid data."""
        canvas.delete("cells")
        canvas.delete("cells_sunk_ship_line")

        grid_origin_x = self.cell_size
        grid_origin_y = self.cell_size
        
        base_dot_radius = self.cell_size * 0.15
        ship_ring_outer_radius = self.cell_size * 0.30
        ship_ring_thickness = self.cell_size * 0.05
        miss_dot_radius = self.cell_size * 0.25
        hit_x_padding = self.cell_size * 0.2

        water_bg_color = "#4682B4"

        for r, row_data in enumerate(grid_data):
            if r >= self.board_size: continue
            for c, cell_char in enumerate(row_data):
                if c >= self.board_size: continue
                
                x0_rect = grid_origin_x + c * self.cell_size
                y0_rect = grid_origin_y + r * self.cell_size
                x1_rect = x0_rect + self.cell_size
                y1_rect = y0_rect + self.cell_size
                
                center_x = x0_rect + self.cell_size / 2
                center_y = y0_rect + self.cell_size / 2

                canvas.create_rectangle(x0_rect, y0_rect, x1_rect, y1_rect, fill=water_bg_color, outline=water_bg_color, tags="cells")

                canvas.create_oval(center_x - base_dot_radius, center_y - base_dot_radius,
                                    center_x + base_dot_radius, center_y + base_dot_radius,
                                    fill='#E0E0E0', outline='#E0E0E0', tags="cells")

                if cell_char == 'S': # Ship
                    canvas.create_oval(center_x - ship_ring_outer_radius, center_y - ship_ring_outer_radius,
                                        center_x + ship_ring_outer_radius, center_y + ship_ring_outer_radius,
                                        fill='purple', outline='purple', tags="cells")
                    canvas.create_oval(center_x - (ship_ring_outer_radius - ship_ring_thickness), 
                                        center_y - (ship_ring_outer_radius - ship_ring_thickness),
                                        center_x + (ship_ring_outer_radius - ship_ring_thickness), 
                                        center_y + (ship_ring_outer_radius - ship_ring_thickness),
                                        fill='purple', outline='purple', tags="cells")

                elif cell_char == 'o': # Miss
                    canvas.create_oval(center_x - miss_dot_radius, center_y - miss_dot_radius,
                                        center_x + miss_dot_radius, center_y + miss_dot_radius,
                                        fill='white', outline='white', tags="cells")

                elif cell_char == 'X': # Hit
                    canvas.create_line(x0_rect + hit_x_padding, y0_rect + hit_x_padding,
                                        x1_rect - hit_x_padding, y1_rect - hit_x_padding,
                                        fill='#DC143C', width=3, tags="cells")
                    canvas.create_line(x0_rect + hit_x_padding, y1_rect - hit_x_padding,
                                        x1_rect - hit_x_padding, y0_rect + hit_x_padding,
                                        fill='#DC143C', width=3, tags="cells")

        # Draw strike-through for sunk ships
        sunk_ships_to_draw_lines_for = None
        if canvas == self.player_board_canvas:
            sunk_ships_to_draw_lines_for = self.sunk_ships_on_my_board_coords
        elif canvas == self.opponent_board_canvas:
            sunk_ships_to_draw_lines_for = self.sunk_ships_on_opponent_board_coords

        if sunk_ships_to_draw_lines_for:
            for ship_coords_set in sunk_ships_to_draw_lines_for:
                if len(ship_coords_set) > 1:
                    coords_list = sorted(list(ship_coords_set))

                    first_r, first_c = coords_list[0]
                    last_r, last_c = coords_list[-1]

                    start_x = grid_origin_x + first_c * self.cell_size + self.cell_size / 2
                    start_y = grid_origin_y + first_r * self.cell_size + self.cell_size / 2
                    end_x = grid_origin_x + last_c * self.cell_size + self.cell_size / 2
                    end_y = grid_origin_y + last_r * self.cell_size + self.cell_size / 2

                    canvas.create_line(start_x, start_y, end_x, end_y,
                                     fill='#FF0000', width=max(2, int(self.cell_size * 0.1)), tags="cells_sunk_ship_line")

    def _send_chat(self):
        """Handles sending chat messages."""
        if not self.sock or not self.running:
            self.log_command("[ERROR] Not connected to server.", msg_type="error")
            return

        user_input = self.chat_input.get().strip()
        if not user_input:
            return

        self.chat_input.delete(0, tk.END)

        if user_input.lower() == 'quit':
            self.log_command("[INFO] Quitting the game...", msg_type="info")
            if self.sock: send_packet(self.sock, PACKET_TYPE_DISCONNECT, "Quit requested by user")
            self._shutdown_client(save_info=False)
            return

        if self.sock:
            if send_packet(self.sock, PACKET_TYPE_CHAT, user_input):
                display_name = self.username
                if self.is_spectator:
                    display_name += " (spectator)"
                self.log_message(f"{display_name}: {user_input}", msg_type="self_chat")
            else:
                self.log_command("[ERROR] Failed to send chat message to server.", msg_type="error")

    def _send_command(self, event=None):
        """Handles sending game commands from the System Info panel."""
        if not self.sock or not self.running:
            self.log_command("[ERROR] Not connected to server.", msg_type="error")
            return

        user_input = self.system_info_input.get().strip()
        if not user_input:
            return

        self.system_info_input.delete(0, tk.END)

        if self.is_spectator:
            self.log_command("[INFO] Spectators cannot make game moves.", msg_type="info")
            return

        if send_packet(self.sock, PACKET_TYPE_MOVE, user_input.upper()):
            self.log_command(f"[ACTION] Sending game command: {user_input.upper()}", msg_type="action_log")
        else:
            self.log_command("[ERROR] Failed to send command to the server.", msg_type="error")

    def log_message(self, message, msg_type=None):
        """Logs a CHAT message to the CHAT display panel.
           Formats the message with sender info if applicable based on msg_type.
        """
        if not self.chat_display.winfo_exists():
            return

        self.chat_display.config(state=tk.NORMAL)
        now = time.strftime("[%H:%M:%S] ", time.localtime())
        self.chat_display.insert(tk.END, now, "timestamp")

        display_text = message
        text_tags = ()

        if msg_type == "self_chat":
            text_tags = ("self_msg_text",)
        elif msg_type == "other_chat":
            text_tags = ("other_msg_text",)
        elif msg_type == "spectator_chat":
            text_tags = ("spectator_msg_text",)

        self.chat_display.insert(tk.END, display_text + "\n", text_tags)
        self.chat_display.config(state=tk.DISABLED)
        self.chat_display.see(tk.END)

    def log_command(self, message, msg_type=None):
        """Logs a command, system, or game event message to the SYSTEM INFO display panel.
           Uses MSG_TYPE_CONFIG for styling if applicable.
        """
        if not self.system_info_display.winfo_exists():
            return
            
        self.system_info_display.config(state=tk.NORMAL)
        now = time.strftime("[%H:%M:%S] ", time.localtime())
        self.system_info_display.insert(tk.END, now, "timestamp")

        config = MSG_TYPE_CONFIG.get(msg_type, MSG_TYPE_CONFIG.get(None))
        sender_name_override = config.get("sender_name_override")
        text_content = message
        sender_tag = config.get("sender_tag")
        base_text_tag = config.get("text_tag", MSG_TYPE_CONFIG.get(None)["text_tag"])

        if config.get("split_message", False) and not sender_name_override and ":" in text_content:
            try:
                parts = text_content.split(":", 1)
                parsed_sender = parts[0].strip()
                parsed_content = parts[1].strip() if len(parts) > 1 else ""
                if parsed_sender and parsed_content:
                    self.system_info_display.insert(tk.END, f"{parsed_sender}: ", sender_tag if sender_tag else base_text_tag)
                    text_content = parsed_content
            except Exception:
                pass
        elif sender_name_override and sender_tag:
            self.system_info_display.insert(tk.END, f"{sender_name_override}: ", sender_tag)
        
        self.system_info_display.insert(tk.END, text_content + "\n", base_text_tag)
        self.system_info_display.config(state=tk.DISABLED)
        self.system_info_display.see(tk.END)

    def _on_closing(self):
        """Handles the window closing event."""
        if messagebox.askokcancel("Quit", "Do you want to quit Battleship?"):
            self.log_command("[INFO] Quit by closing window.", msg_type="info")
            if self.sock:
                 send_packet(self.sock, PACKET_TYPE_DISCONNECT, "Client closed window")
            self._shutdown_client(save_info=True) 

    def _shutdown_client(self, save_info=True):
        """Shuts down the client and cleans up resources."""
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

        if self.winfo_exists():
            self.destroy()


if __name__ == "__main__":
    app = BattleshipGUI()
    try:
        app.mainloop()
    except KeyboardInterrupt:
        if hasattr(app, 'running') and app.running : 
             if hasattr(app, 'log_command'): app.log_command("[INFO] Client exiting due to keyboard interrupt.", msg_type="info")
             if hasattr(app, '_shutdown_client'): app._shutdown_client(save_info=True)
    finally:
        pass