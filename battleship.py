"""
battleship.py

Contains core data structures and logic for Battleship, including:
 - Board class for storing ship positions, hits, misses
 - Utility function parse_coordinate for translating e.g. 'B5' -> (row, col)
 - A test harness run_single_player_game() to demonstrate the logic in a local, single-player mode

"""

import random
import select
import time
import threading

BOARD_SIZE = 10
SHIPS = [
    ("Carrier", 5),
    ("Battleship", 4),
    ("Cruiser", 3),
    ("Submarine", 3),
    ("Destroyer", 2)
]

# Define a constant for move timeout to match with server
MOVE_TIMEOUT = 30  # seconds a player has to make a move

class Board:
    """
    Represents a single Battleship board with hidden ships.
    We store:
      - self.hidden_grid: tracks real positions of ships ('S'), hits ('X'), misses ('o')
      - self.display_grid: the version we show to the player ('.' for unknown, 'X' for hits, 'o' for misses)
      - self.placed_ships: a list of dicts, each dict with:
          {
             'name': <ship_name>,
             'positions': set of (r, c),
          }
        used to determine when a specific ship has been fully sunk.

    In a full 2-player networked game:
      - Each player has their own Board instance.
      - When a player fires at their opponent, the server calls
        opponent_board.fire_at(...) and sends back the result.
    """

    def __init__(self, size=BOARD_SIZE):
        self.size = size
        # '.' for empty water
        self.hidden_grid = [['.' for _ in range(size)] for _ in range(size)]
        # display_grid is what the player or an observer sees (no 'S')
        self.display_grid = [['.' for _ in range(size)] for _ in range(size)]
        self.placed_ships = []  # e.g. [{'name': 'Destroyer', 'positions': {(r, c), ...}}, ...]
        self.spectators = []

    def place_ships_randomly(self, ships=SHIPS):
        """
        Randomly place each ship in 'ships' on the hidden_grid, storing positions for each ship.
        In a networked version, you might parse explicit placements from a player's commands
        (e.g. "PLACE A1 H BATTLESHIP") or prompt the user for board coordinates and placement orientations; 
        the self.place_ships_manually() can be used as a guide.
        """
        for ship_name, ship_size in ships:
            placed = False
            while not placed:
                orientation = random.randint(0, 1)  # 0 => horizontal, 1 => vertical
                row = random.randint(0, self.size - 1)
                col = random.randint(0, self.size - 1)

                if self.can_place_ship(row, col, ship_size, orientation):
                    occupied_positions = self.do_place_ship(row, col, ship_size, orientation)
                    self.placed_ships.append({
                        'name': ship_name,
                        'positions': occupied_positions
                    })
                    placed = True


    def place_ships_manually(self, ships=SHIPS):
        """
        Prompt the user for each ship's starting coordinate and orientation (H or V).
        Validates the placement; if invalid, re-prompts.
        """
        print("\nPlease place your ships manually on the board.")
        for ship_name, ship_size in ships:
            while True:
                self.print_display_grid(show_hidden_board=True)
                print(f"\nPlacing your {ship_name} (size {ship_size}).")
                coord_str = input("  Enter starting coordinate (e.g. A1): ").strip()
                orientation_str = input("  Orientation? Enter 'H' (horizontal) or 'V' (vertical): ").strip().upper()

                try:
                    row, col = parse_coordinate(coord_str)
                except ValueError as e:
                    print(f"  [!] Invalid coordinate: {e}")
                    continue

                # Convert orientation_str to 0 (horizontal) or 1 (vertical)
                if orientation_str == 'H':
                    orientation = 0
                elif orientation_str == 'V':
                    orientation = 1
                else:
                    print("  [!] Invalid orientation. Please enter 'H' or 'V'.")
                    continue

                # Check if we can place the ship
                if self.can_place_ship(row, col, ship_size, orientation):
                    occupied_positions = self.do_place_ship(row, col, ship_size, orientation)
                    self.placed_ships.append({
                        'name': ship_name,
                        'positions': occupied_positions
                    })
                    break
                else:
                    print(f"  [!] Cannot place {ship_name} at {coord_str} (orientation={orientation_str}). Try again.")


    def can_place_ship(self, row, col, ship_size, orientation):
        """
        Check if we can place a ship of length 'ship_size' at (row, col)
        with the given orientation (0 => horizontal, 1 => vertical).
        Returns True if the space is free, False otherwise.
        """
        if orientation == 0:  # Horizontal
            if col + ship_size > self.size:
                return False
            for c in range(col, col + ship_size):
                if self.hidden_grid[row][c] != '.':
                    return False
        else:  # Vertical
            if row + ship_size > self.size:
                return False
            for r in range(row, row + ship_size):
                if self.hidden_grid[r][col] != '.':
                    return False
        return True

    def do_place_ship(self, row, col, ship_size, orientation):
        """
        Place the ship on hidden_grid by marking 'S', and return the set of occupied positions.
        """
        occupied = set()
        if orientation == 0:  # Horizontal
            for c in range(col, col + ship_size):
                self.hidden_grid[row][c] = 'S'
                occupied.add((row, c))
        else:  # Vertical
            for r in range(row, row + ship_size):
                self.hidden_grid[r][col] = 'S'
                occupied.add((r, col))
        return occupied

    def fire_at(self, row, col):
        """
        Fire at (row, col). Return a tuple (result, sunk_ship_name).
        Possible outcomes:
          - ('hit', None)          if it's a hit but not sunk
          - ('hit', <ship_name>)   if that shot causes the entire ship to sink
          - ('miss', None)         if no ship was there
          - ('already_shot', None) if that cell was already revealed as 'X' or 'o'
          - ('invalid', None)      if the coordinates are out of bounds

        The server can use this result to inform the firing player.
        """
        # Validate row and col are within bounds
        if row < 0 or row >= self.size or col < 0 or col >= self.size:
            return ('invalid', None)
            
        cell = self.hidden_grid[row][col]
        if cell == 'S':
            # Mark a hit
            self.hidden_grid[row][col] = 'X'
            self.display_grid[row][col] = 'X'
            # Check if that hit sank a ship
            sunk_ship_name = self._mark_hit_and_check_sunk(row, col)
            if sunk_ship_name:
                return ('hit', sunk_ship_name)  # A ship has just been sunk
            else:
                return ('hit', None)
        elif cell == '.':
            # Mark a miss
            self.hidden_grid[row][col] = 'o'
            self.display_grid[row][col] = 'o'
            return ('miss', None)
        elif cell == 'X' or cell == 'o':
            return ('already_shot', None)
        else:
            # In principle, this branch shouldn't happen if 'S', '.', 'X', 'o' are all possibilities
            return ('already_shot', None)

    def _mark_hit_and_check_sunk(self, row, col):
        """
        Remove (row, col) from the relevant ship's positions.
        If that ship's positions become empty, return the ship name (it's sunk).
        Otherwise return None.
        """
        for ship in self.placed_ships:
            if (row, col) in ship['positions']:
                ship['positions'].remove((row, col))
                if len(ship['positions']) == 0:
                    return ship['name']
                break
        return None

    def all_ships_sunk(self):
        """
        Check if all ships are sunk (i.e. every ship's positions are empty).
        """
        for ship in self.placed_ships:
            if len(ship['positions']) > 0:
                return False
        return True

    def print_display_grid(self, show_hidden_board=False):
        """
        Print the board as a 2D grid.
        
        If show_hidden_board is False (default), it prints the 'attacker' or 'observer' view:
        - '.' for unknown cells,
        - 'X' for known hits,
        - 'o' for known misses.
        
        If show_hidden_board is True, it prints the entire hidden grid:
        - 'S' for ships,
        - 'X' for hits,
        - 'o' for misses,
        - '.' for empty water.
        """
        # Decide which grid to print
        grid_to_print = self.hidden_grid if show_hidden_board else self.display_grid

        # Column headers
        header = "   "
        for i in range(self.size):
            header += f"{i+1}".center(3)
        print(header)
        
        # Each row labeled with A, B, C, ...
        for r in range(self.size):
            row_label = chr(ord('A') + r)
            row_str = ""
            for c in range(self.size):
                row_str += grid_to_print[r][c].center(3)
            print(f"{row_label}  {row_str}")


def parse_coordinate(coord_str):
    """
    Convert something like 'B5' into zero-based (row, col).
    Example: 'A1' => (0, 0), 'C10' => (2, 9)
    HINT: you might want to add additional input validation here...
    """
    coord_str = coord_str.strip().upper()
    print(BOARD_SIZE)
    
    # Check if the input is empty
    if not coord_str:
        raise ValueError("Coordinate cannot be empty")
    
    # Check if first character is a letter
    if not coord_str[0].isalpha():
        raise ValueError("Coordinate must start with a letter (A-J)")
    
    # Check if there are digits after the letter
    if len(coord_str) < 2 or not coord_str[1:].isdigit():
        raise ValueError("Coordinate must have a number after the letter")
    
    row_letter = coord_str[0]
    col_digits = coord_str[1:]
    
    # Validate the column number before converting
    col_num = int(col_digits)
    if col_num < 1 or col_num > BOARD_SIZE:
        raise ValueError(f"Column must be a number between 1 and {BOARD_SIZE}")
    
    # Convert to row, col indices
    row = ord(row_letter) - ord('A')
    col = col_num - 1  # zero-based
    
    # Check if the row is in range (A-J for a 10x10 board)
    if row < 0 or row >= BOARD_SIZE:
        raise ValueError(f"Row must be a letter between A and {chr(ord('A') + BOARD_SIZE - 1)}")
    
    # Check if the column is in range (1-10 for a 10x10 board)
    if col < 0 or col >= BOARD_SIZE:
        print("X")
        raise ValueError(f"Column must be a number between 1 and {BOARD_SIZE}")
    
    return (row, col)


def run_single_player_game_locally():
    """
    A test harness for local single-player mode, demonstrating two approaches:
     1) place_ships_manually()
     2) place_ships_randomly()

    Then the player tries to sink them by firing coordinates.
    """
    board = Board(BOARD_SIZE)

    # Ask user how they'd like to place ships
    choice = input("Place ships manually (M) or randomly (R)? [M/R]: ").strip().upper()
    if choice == 'M':
        board.place_ships_manually(SHIPS)
    else:
        board.place_ships_randomly(SHIPS)

    print("\nNow try to sink all the ships!")
    moves = 0
    while True:
        board.print_display_grid()
        guess = input("\nEnter coordinate to fire at (or 'quit'): ").strip()
        if guess.lower() == 'quit':
            print("Thanks for playing. Exiting...")
            return

        try:
            row, col = parse_coordinate(guess)
            result, sunk_name = board.fire_at(row, col)
            moves += 1

            if result == 'hit':
                if sunk_name:
                    print(f"  >> HIT! You sank the {sunk_name}!")
                else:
                    print("  >> HIT!")
                if board.all_ships_sunk():
                    board.print_display_grid()
                    print(f"\nCongratulations! You sank all ships in {moves} moves.")
                    break
            elif result == 'miss':
                print("  >> MISS!")
            elif result == 'already_shot':
                print("  >> You've already fired at that location. Try again.")

        except ValueError as e:
            print("  >> Invalid input:", e)


def run_single_player_game_online(rfile, wfile):
    """
    A test harness for running the single-player game with I/O redirected to socket file objects.
    Expects:
      - rfile: file-like object to .readline() from client
      - wfile: file-like object to .write() back to client
    
    #####
    NOTE: This function is (intentionally) currently somewhat "broken", which will be evident if you try and play the game via server/client.
    You can use this as a starting point, or write your own.
    #####
    """
    def send(msg):
        wfile.write(msg + '\n')
        wfile.flush()

    def send_board(board):
        wfile.write("GRID\n")
        wfile.write("  " + " ".join(str(i + 1).rjust(2) for i in range(board.size)) + '\n')
        for r in range(board.size):
            row_label = chr(ord('A') + r)
            row_str = " ".join(board.display_grid[r][c] for c in range(board.size))
            wfile.write(f"{row_label:2} {row_str}\n")
        wfile.write('\n')
        wfile.flush()

    def recv():
        return rfile.readline().strip()

    board = Board(BOARD_SIZE)
    board.place_ships_randomly(SHIPS)

    send("Welcome to Online Single-Player Battleship! Try to sink all the ships. Type 'quit' to exit.")

    moves = 0
    while True:
        send_board(board)
        send("Enter coordinate to fire at (e.g. B5):")
        guess = recv()
        if guess.lower() == 'quit':
            send("Thanks for playing. Goodbye.")
            return

        try:
            row, col = parse_coordinate(guess)
            result, sunk_name = board.fire_at(row, col)
            moves += 1

            if result == 'hit':
                if sunk_name:
                    send(f"HIT! You sank the {sunk_name}!")
                else:
                    send("HIT!")
                if board.all_ships_sunk():
                    send_board(board)
                    send(f"Congratulations! You sank all ships in {moves} moves.")
                    return
            elif result == 'miss':
                send("MISS!")
            elif result == 'already_shot':
                send("You've already fired at that location.")
        except ValueError as e:
            send(f"Invalid input: {e}")


def run_two_player_game(player1_rfile, player1_wfile, player2_rfile, player2_wfile, notify_spectators_callback):
    """
    Run a two-player Battleship game with I/O redirected to socket file objects.
    Each player takes turns firing at their opponent's board.
    Includes a timeout mechanism to handle inactive players.
    
    Expects:
      - player1_rfile/player1_wfile: File-like objects or protocol adapters for player1
      - player2_rfile/player2_wfile: File-like objects or protocol adapters for player2
      - notify_spectators_callback: A function to call to send messages to spectators
    """
    # Existing send_to_player and send_board_to_player functions remain the same
    # as they now work with either file objects or our protocol adapters
    
    # Create spectator notification wrapper function that works with both
    # old text protocol and new binary protocol
    def notify_spectators_wrapper(message):
        """
        Wrapper around notify_spectators_callback to handle protocol conversion if needed.
        """
        # Call the provided spectator notification callback
        notify_spectators_callback(message)
    
    # Timeout settings are defined at the module level
    
    def send_to_player(player_wfile, msg):
        try:
            player_wfile.write(msg + '\n')
            player_wfile.flush()
        except Exception as e:
            print(f"Error sending to player: {e}")
    
    def send_board_to_player(player_wfile, own_board, opponent_board=None):
        try:
            # First send player's own board (with ships visible)
            player_wfile.write("Your Grid:\n")
            
            # Column headers
            header = "   "
            for i in range(own_board.size):
                header += f"{i+1}".center(3)
            player_wfile.write(header + "\n")
            
            # Generate rows with proper spacing
            for r in range(own_board.size):
                row_label = chr(ord('A') + r)
                row_str = ""
                for c in range(own_board.size):
                    row_str += own_board.hidden_grid[r][c].center(3)
                player_wfile.write(f"{row_label}  {row_str}\n")
            
            player_wfile.write('\n')
            player_wfile.flush()
            
            # Then send opponent's board (only hits/misses visible) if provided
            if opponent_board:
                player_wfile.write("Opponent's Grid:\n")
                
                # Column headers
                header = "   "
                for i in range(opponent_board.size):
                    header += f"{i+1}".center(3) 
                player_wfile.write(header + "\n")
                
                # Generate rows
                for r in range(opponent_board.size):
                    row_label = chr(ord('A') + r)
                    row_str = ""
                    for c in range(opponent_board.size):
                        row_str += opponent_board.display_grid[r][c].center(3)
                    player_wfile.write(f"{row_label}  {row_str}\n")
                
                player_wfile.write('\n')
                player_wfile.flush()
            
        except Exception as e:
            print(f"Error sending board to player: {e}")

    def send_board_to_spectators(player1_board, player2_board, notify_spectators_callback_func):
        """
        Send both players' boards to spectators.
        Spectators see both boards with ships hidden.
        """
        try:
            # Prepare the board data as a single formatted string
            board_data = []
            board_data.append("SPECTATOR_GRID\n")
            
            # Add Player 1's board
            board_data.append("Player 1's Board:\n")
            
            # Column headers
            header = "   "
            for i in range(player1_board.size):
                header += f"{i+1}".center(3)
            board_data.append(header + "\n")
            
            for r in range(player1_board.size):
                row_label = chr(ord('A') + r)
                row_str = ""
                for c in range(player1_board.size):
                    row_str += player1_board.display_grid[r][c].center(3)
                board_data.append(f"{row_label}  {row_str}\n")
            board_data.append("\n")
            
            # Add Player 2's board
            board_data.append("Player 2's Board:\n")
            
            # Column headers
            header = "   "
            for i in range(player2_board.size):
                header += f"{i+1}".center(3)
            board_data.append(header + "\n")
            
            for r in range(player2_board.size):
                row_label = chr(ord('A') + r)
                row_str = ""
                for c in range(player2_board.size):
                    row_str += player2_board.display_grid[r][c].center(3)
                board_data.append(f"{row_label}  {row_str}\n")
            board_data.append("\n")
            
            # Send the combined board data
            notify_spectators_callback_func("".join(board_data))
        except Exception as e:
            print(f"Error sending board to spectators: {e}")

    def recv_from_player_with_timeout(player_rfile, timeout_secs):
        """
        Receive input from a player with a timeout.
        Returns the input string or None if timeout occurs.
        
        Works with both file-like objects and protocol adapters.
        """
        max_attempts = 3
        attempt = 0
        
        try:
            # If player_rfile is a protocol adapter (has no fileno method), use its readline method
            if not hasattr(player_rfile, 'fileno'):
                while attempt < max_attempts:
                    response = player_rfile.readline().strip()
                    # Handle empty responses - this happens when the adapter receives
                    # a packet that doesn't translate to a valid game input
                    if response:
                        return response
                    
                    time.sleep(0.1)
                    attempt += 1
                
                return None
            
            fd = player_rfile.fileno()
            # Wait for the file descriptor to be ready for reading
            ready, _, _ = select.select([fd], [], [], timeout_secs)
            if ready:
                # Data is available, read it
                return player_rfile.readline().strip()
            else:
                # Timeout occurred
                return None
        except Exception as e:
            print(f"Error receiving from player: {e}")
            return None
    
    def handle_ship_placement(player_rfile, player_wfile, player_board, player_name):
        """
        Handle the ship placement phase for a player.
        Returns True if ships were placed successfully (manually or randomly), False if player quit.
        """
        send_to_player(player_wfile, f"{player_name}, it's time to place your ships!")
        send_to_player(player_wfile, "Would you like to place ships manually (M) or randomly (R)? [M/R]:")
        
        choice = recv_from_player_with_timeout(player_rfile, MOVE_TIMEOUT)
        
        # If timeout or no input, default to random placement
        if choice is None:
            send_to_player(player_wfile, "No selection made within timeout period. Ships will be placed randomly.")
            player_board.place_ships_randomly(SHIPS)
            send_to_player(player_wfile, "Ships have been placed randomly on your board.")
            send_board_to_player(player_wfile, player_board)
            return True

        choice = choice.upper()[0] if choice else ""
        
        if choice == 'M':
            # Manual placement
            for ship_name, ship_size in SHIPS:
                placed = False
                while not placed:
                    # Show current board state
                    send_board_to_player(player_wfile, player_board)
                    send_to_player(player_wfile, f"Placing your {ship_name} (size {ship_size}).")
                    send_to_player(player_wfile, "Enter starting coordinate (e.g. A1):")
                    coord_str = recv_from_player_with_timeout(player_rfile, MOVE_TIMEOUT)
                    
                    # Check for timeout or quit command
                    if coord_str is None:
                        send_to_player(player_wfile, f"Timeout waiting for coordinate. {ship_name} will be placed randomly.")
                        # Place this ship randomly and continue with next ship
                        randomly_place_single_ship(player_board, ship_name, ship_size)
                        send_to_player(player_wfile, f"{ship_name} placed randomly.")
                        placed = True
                        continue
                    elif coord_str.lower() == 'quit':
                        return False
                    
                    send_to_player(player_wfile, "Orientation? Enter 'H' (horizontal) or 'V' (vertical):")
                    orientation_str = recv_from_player_with_timeout(player_rfile, MOVE_TIMEOUT)
                    
                    # Check for timeout or quit command
                    if orientation_str is None:
                        send_to_player(player_wfile, f"Timeout waiting for orientation. {ship_name} will be placed randomly.")
                        # Place this ship randomly and continue with next ship
                        randomly_place_single_ship(player_board, ship_name, ship_size)
                        send_to_player(player_wfile, f"{ship_name} placed randomly.")
                        placed = True
                        continue
                    elif orientation_str.lower() == 'quit':
                        return False
                    
                    try:
                        row, col = parse_coordinate(coord_str)
                        
                        # Normalize the orientation string
                        orientation_str = orientation_str.upper()[0] if orientation_str else ""
                        
                        # Convert orientation_str to 0 (horizontal) or 1 (vertical)
                        if orientation_str == 'H':
                            orientation = 0
                        elif orientation_str == 'V':
                            orientation = 1
                        else:
                            send_to_player(player_wfile, "Invalid orientation. Please enter 'H' or 'V'.")
                            continue
                        
                        if player_board.can_place_ship(row, col, ship_size, orientation):
                            occupied_positions = player_board.do_place_ship(row, col, ship_size, orientation)
                            player_board.placed_ships.append({
                                'name': ship_name,
                                'positions': occupied_positions
                            })
                            send_to_player(player_wfile, f"{ship_name} placed successfully!")
                            placed = True
                        else:
                            send_to_player(player_wfile, f"Cannot place {ship_name} at {coord_str} (orientation={orientation_str}). Try again.")
                    
                    except ValueError as e:
                        send_to_player(player_wfile, f"Invalid input: {e}. Try again.")
            
            send_to_player(player_wfile, "All ships placed successfully!")
            send_board_to_player(player_wfile, player_board)
            return True
        else:
            # Random placement (any non-M input)
            player_board.place_ships_randomly(SHIPS)
            send_to_player(player_wfile, "Ships have been placed randomly on your board.")
            send_board_to_player(player_wfile, player_board)
            return True  # Return True for successful random placement

    def randomly_place_single_ship(board, ship_name, ship_size):
        """
        Randomly place a single ship on the board.
        Used when a player times out during manual placement.
        """
        placed = False
        while not placed:
            orientation = random.randint(0, 1)  # 0 => horizontal, 1 => vertical
            row = random.randint(0, board.size - 1)
            col = random.randint(0, board.size - 1)

            if board.can_place_ship(row, col, ship_size, orientation):
                occupied_positions = board.do_place_ship(row, col, ship_size, orientation)
                board.placed_ships.append({
                    'name': ship_name,
                    'positions': occupied_positions
                })
                placed = True
    
    # Create boards for each player
    player1_board = Board(BOARD_SIZE)
    player2_board = Board(BOARD_SIZE)
    
    # Welcome messages
    send_to_player(player1_wfile, "Welcome to Battleship! You are Player 1. Waiting for Player 2 to join...")
    send_to_player(player2_wfile, "Welcome to Battleship! You are Player 2. Game is starting...")
    
    # Notify both players that game is starting
    send_to_player(player1_wfile, "Game is starting! Player 2 has joined.")
    send_to_player(player2_wfile, "Game is starting! You are playing against Player 1.")
    
    # Ship placement phase
    send_to_player(player1_wfile, "Starting ship placement phase...")
    send_to_player(player2_wfile, "Waiting for Player 1 to place ships...")
    
    if not handle_ship_placement(player1_rfile, player1_wfile, player1_board, "Player 1"):
        send_to_player(player1_wfile, "You have quit during ship placement. Game ending.")
        send_to_player(player2_wfile, "Player 1 has quit during ship placement. Game ending.")
        return
    
    send_to_player(player1_wfile, "Waiting for Player 2 to place ships...")
    send_to_player(player2_wfile, "Player 1 has placed their ships. Now it's your turn.")
    
    if not handle_ship_placement(player2_rfile, player2_wfile, player2_board, "Player 2"):
        send_to_player(player1_wfile, "Player 2 has quit during ship placement. Game ending.")
        send_to_player(player2_wfile, "You have quit during ship placement. Game ending.")
        return
    
    # Notify both players that the firing phase is starting
    send_to_player(player1_wfile, "All ships have been placed. Starting the game!")
    send_to_player(player2_wfile, "All ships have been placed. Starting the game!")
    
    # Main game loop - alternate between players
    current_player = 1  # Start with player 1
    consecutive_timeouts = 0  # Count consecutive timeouts to prevent infinite loops
    
    while True:
        # Determine current player's files and boards
        if current_player == 1:
            current_rfile, current_wfile = player1_rfile, player1_wfile
            other_rfile, other_wfile = player2_rfile, player2_wfile
            current_board, opponent_board = player1_board, player2_board
            current_player_name, opponent_name = "Player 1", "Player 2"
        else:
            current_rfile, current_wfile = player2_rfile, player2_wfile
            other_rfile, other_wfile = player1_rfile, player1_wfile
            current_board, opponent_board = player2_board, player1_board
            current_player_name, opponent_name = "Player 2", "Player 1"
        
        # Notify players about whose turn it is
        send_to_player(current_wfile, f"It\'s your turn, {current_player_name}!")
        send_to_player(other_wfile, f"Waiting for {current_player_name} to make a move...")
        
        # Send board states to current player
        send_board_to_player(current_wfile, current_board, opponent_board)
        
        # Prompt for a move
        send_to_player(current_wfile, f"Enter coordinate to fire at (e.g. B5): (You have {MOVE_TIMEOUT} seconds)")
        
        # Get the move with timeout
        guess = recv_from_player_with_timeout(current_rfile, MOVE_TIMEOUT)
        
        # Handle timeout case
        if guess is None:
            consecutive_timeouts += 1
            
            # If too many consecutive timeouts, end the game
            if consecutive_timeouts >= 3:
                send_to_player(current_wfile, "You have timed out too many times. You forfeit the game.")
                send_to_player(other_wfile, f"{current_player_name} has timed out too many times and forfeited. You win!")
                return
                
            send_to_player(current_wfile, f"You took too long to make a move. Your turn is skipped. You have {3 - consecutive_timeouts} timeouts remaining.")
            send_to_player(other_wfile, f"{current_player_name} timed out and their turn was skipped.")
            
            # Skip to next player's turn
            current_player = 2 if current_player == 1 else 1
            continue
        
        # Reset consecutive timeouts counter if a valid move was made
        consecutive_timeouts = 0
        
        if guess.lower() == 'quit':
            send_to_player(current_wfile, "You have quit the game. Your opponent wins by default.")
            send_to_player(other_wfile, f"{current_player_name} has quit. You win by default!")
            return
        
        try:
            row, col = parse_coordinate(guess)
            result, sunk_name = opponent_board.fire_at(row, col)
            
            # Inform both players of the result
            if result == 'hit':
                if sunk_name:
                    send_to_player(current_wfile, f"HIT! You sank {opponent_name}\'s {sunk_name}!")
                    send_to_player(other_wfile, f"{current_player_name} fired at {guess} and sank your {sunk_name}!")
                    notify_spectators_wrapper(f"{current_player_name} fired at {guess} and sank {opponent_name}\'s {sunk_name}!")
                else:
                    send_to_player(current_wfile, "HIT!")
                    send_to_player(other_wfile, f"{current_player_name} fired at {guess} and scored a hit!")
                    notify_spectators_wrapper(f"{current_player_name} fired at {guess} and scored a hit!")
                
                # Check if all ships are sunk
                if opponent_board.all_ships_sunk():
                    # Show both boards to both players one final time
                    send_board_to_player(current_wfile, current_board, opponent_board)
                    send_board_to_player(other_wfile, opponent_board, current_board)
                    send_board_to_spectators(current_board, opponent_board, notify_spectators_wrapper)
                    
                    # Send victory/defeat messages
                    send_to_player(current_wfile, f"Congratulations! You\'ve sunk all of {opponent_name}\'s ships. You win!")
                    send_to_player(other_wfile, f"Game over! {current_player_name} has sunk all your ships.")
                    notify_spectators_wrapper(f"Game over! {current_player_name} has won by sinking all of {opponent_name}\'s ships!")
                    return
            elif result == 'miss':
                send_to_player(current_wfile, "MISS!")
                send_to_player(other_wfile, f"{current_player_name} fired at {guess} and missed!")
                notify_spectators_wrapper(f"{current_player_name} fired at {guess} and missed!")
            elif result == 'already_shot':
                send_to_player(current_wfile, "You\'ve already fired at that location. Try again.")
                # Don't switch players for an invalid move
                continue
            elif result == 'invalid':
                send_to_player(current_wfile, "Invalid coordinate. Please enter a valid coordinate (e.g. A1-J10).")
                # Don't switch players for an invalid move
                continue
                
            print("DEBUG: Player made a move:", guess)
            print("DEBUG: Result:", result, "Sunk:", sunk_name)
            
            # Send updated boards to spectators after each move
            send_board_to_spectators(current_board, opponent_board, notify_spectators_wrapper)
                
        except ValueError as e:
            send_to_player(current_wfile, f"Invalid input: {e}. Try again.")
            # Don't switch players for an invalid move
            continue
        
        # Switch to the other player for the next turn
        current_player = 2 if current_player == 1 else 1


def handle_spectator(conn, addr, spectators):
    rfile = conn.makefile('r')
    wfile = conn.makefile('w')
    spectators.append((conn, wfile))
    try:
        wfile.write("[INFO] You are now spectating the current game.\n")
        wfile.flush()
        while True:
            # Keep the connection alive, or allow 'quit' to leave
            if rfile.readline().strip().lower() == 'quit':
                break
    except:
        pass
    finally:
        spectators.remove((conn, wfile))
        conn.close()


if __name__ == "__main__":
    # Optional: run this file as a script to test single-player mode
    run_single_player_game_locally()

