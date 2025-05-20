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

MOVE_TIMEOUT = 30  # make a move time limit

# Custom exception for player disconnections
class PlayerDisconnectedError(Exception):
    def __init__(self, player_name, game_state):
        self.player_name = player_name
        self.game_state = game_state
        super().__init__(f"Player {player_name} disconnected.")


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
        self.display_grid = [['.' for _ in range(size)] for _ in range(size)]
        self.placed_ships = []
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

                # Check if ship can be placed
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

    def serialize(self):
        """Return a dictionary representing the board's state."""
        # Convert sets of tuples to lists of lists for JSON compatibility
        serialized_placed_ships = []
        for ship in self.placed_ships:
            serialized_ship = {
                'name': ship['name'],
                'positions': [list(pos) for pos in ship['positions']]
            }
            serialized_placed_ships.append(serialized_ship)

        return {
            'size': self.size,
            'hidden_grid': self.hidden_grid,
            'display_grid': self.display_grid,
            'placed_ships': serialized_placed_ships
        }

    @classmethod
    def deserialize(cls, data):
        """Create a Board instance from a serialized dictionary."""
        board_size = data.get('size', BOARD_SIZE)
        board = cls(board_size)
        board.hidden_grid = data['hidden_grid']
        board.display_grid = data['display_grid']
        
        deserialized_placed_ships = []
        for ship_data in data['placed_ships']:
            deserialized_ship = {
                'name': ship_data['name'],
                'positions': {tuple(pos) for pos in ship_data['positions']}
            }
            deserialized_placed_ships.append(deserialized_ship)
        board.placed_ships = deserialized_placed_ships
        
        return board


def parse_coordinate(coord_str):
    """
    Convert e.g., 'B5' into zero-based (row, col).
    """
    coord_str = coord_str.strip().upper()
    print(BOARD_SIZE)
    
    if not coord_str:
        raise ValueError("Coordinate cannot be empty")
    
    if not coord_str[0].isalpha():
        raise ValueError("Coordinate must start with a letter (A-J)")
    
    if len(coord_str) < 2 or not coord_str[1:].isdigit():
        raise ValueError("Coordinate must have a number after the letter")
    
    row_letter = coord_str[0]
    col_digits = coord_str[1:]
    
    col_num = int(col_digits)
    if col_num < 1 or col_num > BOARD_SIZE:
        raise ValueError(f"Column must be a number between 1 and {BOARD_SIZE}")
    
    # Convert to row, col indices
    row = ord(row_letter) - ord('A')
    col = col_num - 1
    
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


def recv_from_player_with_timeout(player_rfile, timeout_secs, player_name_for_error="Player"):
    """
    Receive input from a player with a timeout.
    Returns the input string or raises PlayerDisconnectedError if timeout/error occurs.
    
    Works with both file-like objects and protocol adapters.
    """
    try:
        if not hasattr(player_rfile, 'fileno'):
            response = player_rfile.readline().strip()
            if response:
                return response
            else:
                raise PlayerDisconnectedError(player_name_for_error, None)

        fd = player_rfile.fileno()
        ready, _, _ = select.select([fd], [], [], timeout_secs)
        if ready:
            line = player_rfile.readline()
            if not line:
                raise PlayerDisconnectedError(player_name_for_error, None)
            return line.strip()
        else:
            # Timeout occurred
            raise PlayerDisconnectedError(player_name_for_error, None)
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        print(f"Connection error receiving from {player_name_for_error}: {e}")
        raise PlayerDisconnectedError(player_name_for_error, None)
    except PlayerDisconnectedError:
        raise
    except Exception as e:
        print(f"Unexpected error receiving from {player_name_for_error}: {e}")
        raise PlayerDisconnectedError(player_name_for_error, None)


def run_two_player_game(player1_rfile, player1_wfile, player2_rfile, player2_wfile, 
                        notify_spectators_callback,
                        player1_username="Player 1", player2_username="Player 2",
                        initial_player1_board_state=None, 
                        initial_player2_board_state=None,
                        initial_current_player_name=None):
    """
    Run a two-player Battleship game. Can start fresh or resume from a state.
    """
    
    def send_to_player(player_wfile, msg):
        """
        Sends a message to the player. Should work upon each move, but somewhat buggy.
        """
        try:
            player_wfile.write(msg + '\n')
            if not (hasattr(player_wfile, 'grid_mode') and player_wfile.grid_mode):
                if hasattr(player_wfile, 'flush'):
                     player_wfile.flush()
        except PlayerDisconnectedError:
            raise
        except Exception as e:
            player_name = getattr(player_wfile, 'username', 'UnknownPlayer')
            print(f"Error sending message to {player_name}: {e}")
            raise PlayerDisconnectedError(player_name, None) from e

    def send_board_to_player(player_wfile, own_board, opponent_board=None):
        """
        Sends board state(s) to the player.
        """
        player_name = getattr(player_wfile, 'username', 'UnknownPlayer')
        try:
            # Send player's own board
            player_wfile.write("Your Grid:\n")
            header = "   " + "".join(f"{i+1}".center(3) for i in range(own_board.size))
            player_wfile.write(header + "\n")
            for r_idx, r_val in enumerate(own_board.hidden_grid):
                row_label = chr(ord('A') + r_idx)
                row_str = "".join(cell.center(3) for cell in r_val)
                player_wfile.write(f"{row_label}  {row_str}\n")
            player_wfile.write('\n')
            player_wfile.flush()

            # Send opponent's board
            if opponent_board:
                player_wfile.write("Opponent's Grid:\n")
                header = "   " + "".join(f"{i+1}".center(3) for i in range(opponent_board.size))
                player_wfile.write(header + "\n")
                for r_idx, r_val in enumerate(opponent_board.display_grid):
                    row_label = chr(ord('A') + r_idx)
                    row_str = "".join(cell.center(3) for cell in r_val)
                    player_wfile.write(f"{row_label}  {row_str}\n")
                player_wfile.write('\n')
                player_wfile.flush()

        except PlayerDisconnectedError:
            raise
        except Exception as e:
            print(f"Error sending board to {player_name}: {e}")
            raise PlayerDisconnectedError(player_name, None) from e
            
    def send_board_to_spectators(p1_board_obj, p2_board_obj, notify_callback_func):
        """Sends board state(s) to spectators."""
        try:
            board_data = []
            board_data.append("SPECTATOR_GRID\n")
            
            # Add Player 1's board 
            board_data.append(f"{player1_username}'s Grid:\n")
            header = "   "
            for i in range(p1_board_obj.size):
                header += f"{i+1}".center(3)
            board_data.append(header + "\n")
            for r in range(p1_board_obj.size):
                row_label = chr(ord('A') + r)
                row_str = ""
                for c in range(p1_board_obj.size):
                    row_str += p1_board_obj.display_grid[r][c].center(3)
                board_data.append(f"{row_label}  {row_str}\n")
            board_data.append("\n")
            
            # Add Player 2's board 
            board_data.append(f"{player2_username}'s Grid:\n")
            header = "   "
            for i in range(p2_board_obj.size):
                header += f"{i+1}".center(3)
            board_data.append(header + "\n")
            for r in range(p2_board_obj.size):
                row_label = chr(ord('A') + r)
                row_str = ""
                for c in range(p2_board_obj.size):
                    row_str += p2_board_obj.display_grid[r][c].center(3)
                board_data.append(f"{row_label}  {row_str}\n")
            board_data.append("\n")
            
            notify_callback_func("".join(board_data))
        except Exception as e:
            print(f"Error sending board to spectators: {e}")
            
    def handle_ship_placement(player_rfile, player_wfile, player_board_obj, p_name, opponent_board):
        """Handles the ship placement phase of a game session."""
        send_to_player(player_wfile, f"{p_name}, it's time to place your ships!")
        send_to_player(player_wfile, "Would you like to place ships manually (M) or randomly (R)? [M/R]:")
        
        choice = None
        try:
            choice = recv_from_player_with_timeout(player_rfile, MOVE_TIMEOUT, p_name)
        except PlayerDisconnectedError:
            raise

        if choice is None:
            send_to_player(player_wfile, "No selection made within timeout period. Ships will be placed randomly.")
            player_board_obj.place_ships_randomly(SHIPS)
            send_to_player(player_wfile, "Ships have been placed randomly on your board.")
            send_board_to_player(player_wfile, player_board_obj, opponent_board)
            return True

        choice = choice.upper()[0] if choice else ""
        
        if choice == 'M':
            for ship_name, ship_size in SHIPS:
                placed = False
                while not placed:
                    send_board_to_player(player_wfile, player_board_obj, opponent_board) 
                    send_to_player(player_wfile, f"Placing your {ship_name} (size {ship_size}).")
                    send_to_player(player_wfile, "Enter starting coordinate and orientation (e.g. A1 H or B2 V):")
                    
                    combined_input = None
                    try:
                        combined_input = recv_from_player_with_timeout(player_rfile, MOVE_TIMEOUT, p_name)
                    except PlayerDisconnectedError:
                        raise

                    if combined_input is None:
                        send_to_player(player_wfile, f"Timeout waiting for input. {ship_name} will be placed randomly.")
                        randomly_place_single_ship(player_board_obj, ship_name, ship_size)
                        send_to_player(player_wfile, f"{ship_name} placed randomly.")
                        placed = True
                        continue
                    elif combined_input.lower() == 'quit':
                        raise PlayerDisconnectedError(p_name, None)

                    try:
                        parts = combined_input.strip().upper().split()
                        if len(parts) != 2:
                            send_to_player(player_wfile, "Invalid format. Expected coordinate and orientation (e.g., A1 H).")
                            continue
                        
                        coord_str, orientation_char = parts[0], parts[1]
                        row, col = parse_coordinate(coord_str)
                        orientation_enum = 0 if orientation_char == 'H' else (1 if orientation_char == 'V' else -1)

                        if orientation_enum == -1:
                            send_to_player(player_wfile, "Invalid orientation. Please enter 'H' or 'V'.")
                            continue
                        
                        if player_board_obj.can_place_ship(row, col, ship_size, orientation_enum):
                            occupied_positions = player_board_obj.do_place_ship(row, col, ship_size, orientation_enum)
                            player_board_obj.placed_ships.append({'name': ship_name, 'positions': occupied_positions})
                            send_to_player(player_wfile, f"{ship_name} placed successfully!")
                            placed = True
                        else:
                            send_to_player(player_wfile, f"Cannot place {ship_name} at {coord_str} (orientation={orientation_char}). Try again.")
                    except ValueError as e:
                        send_to_player(player_wfile, f"Invalid input: {e}. Try again.")
            
            send_to_player(player_wfile, "All ships placed successfully!")
            send_board_to_player(player_wfile, player_board_obj, opponent_board)
            return True
        else:
            player_board_obj.place_ships_randomly(SHIPS)
            send_to_player(player_wfile, "Ships have been placed randomly on your board.")
            send_board_to_player(player_wfile, player_board_obj, opponent_board)
            return True

    def randomly_place_single_ship(board, ship_name, ship_size):
        placed = False
        while not placed:
            orientation = random.randint(0, 1)
            row = random.randint(0, board.size - 1)
            col = random.randint(0, board.size - 1)

            if board.can_place_ship(row, col, ship_size, orientation):
                occupied_positions = board.do_place_ship(row, col, ship_size, orientation)
                board.placed_ships.append({
                    'name': ship_name,
                    'positions': occupied_positions
                })
                placed = True

    player1_board = Board(BOARD_SIZE)
    player2_board = Board(BOARD_SIZE)

    is_resumed_game = bool(initial_player1_board_state and initial_player2_board_state and initial_current_player_name)

    if is_resumed_game:
        try:
            player1_board = Board.deserialize(initial_player1_board_state)
            player2_board = Board.deserialize(initial_player2_board_state)
            send_to_player(player1_wfile, "Game resumed.")
            send_to_player(player2_wfile, "Game resumed.")
            notify_spectators_callback("Game has been resumed.")
            if not player1_board.placed_ships or not player2_board.placed_ships :
                 print("[GAME LOGIC WARNING] Resumed game but one or both players appear to have no ships placed.")
        except Exception as e:
            is_resumed_game = False
            player1_board = Board(BOARD_SIZE)
            player2_board = Board(BOARD_SIZE)

    if not is_resumed_game:
        send_to_player(player1_wfile, f"Welcome to Battleship! You are {player1_username}. Waiting for {player2_username} to be ready...")
        send_to_player(player2_wfile, f"Welcome to Battleship! You are {player2_username}. Game is starting with {player1_username}.")
        
        notify_spectators_callback(f"New game starting between {player1_username} and {player2_username}.")
        
        send_to_player(player1_wfile, "Starting ship placement phase...")
        send_to_player(player2_wfile, f"Waiting for {player1_username} to place ships...")
        
        try:
            if not handle_ship_placement(player1_rfile, player1_wfile, player1_board, player1_username, player2_board):
                 raise PlayerDisconnectedError(player1_username, {
                    'player1_board_state': player1_board.serialize(),
                    'player2_board_state': player2_board.serialize(),
                    'next_turn_username': player1_username 
                })
        except PlayerDisconnectedError as pde:
            if not pde.game_state:
                next_turn = player1_username if pde.player_name == player2_username else player2_username 
                if 'current_player_name_for_turn' in locals() and current_player_name_for_turn:
                    next_turn = current_player_name_for_turn
                pde.game_state = {
                    'player1_board_state': player1_board.serialize(),
                    'player2_board_state': player2_board.serialize(),
                    'next_turn_username': next_turn
                }
            raise
        except Exception as e:
            raise

        send_to_player(player1_wfile, f"Waiting for {player2_username} to place ships...")
        send_to_player(player2_wfile, f"{player1_username} has placed their ships. Now it's your turn.")
        
        try:
            if not handle_ship_placement(player2_rfile, player2_wfile, player2_board, player2_username, player1_board):
                raise PlayerDisconnectedError(player2_username, {
                    'player1_board_state': player1_board.serialize(),
                    'player2_board_state': player2_board.serialize(),
                    'next_turn_username': player2_username
                })
        except PlayerDisconnectedError as pde:
            if not pde.game_state:
                next_turn = player1_username if pde.player_name == player2_username else player2_username 
                if 'current_player_name_for_turn' in locals() and current_player_name_for_turn:
                    next_turn = current_player_name_for_turn
                pde.game_state = {
                    'player1_board_state': player1_board.serialize(),
                    'player2_board_state': player2_board.serialize(),
                    'next_turn_username': next_turn
                }
            raise
        except Exception as e:
            raise

        send_to_player(player1_wfile, "All ships have been placed. Starting the game!")
        send_to_player(player2_wfile, "All ships have been placed. Starting the game!")
        notify_spectators_callback("Ship placement complete. The game begins!")
    
    current_player_name_for_turn = initial_current_player_name if is_resumed_game else player1_username
    consecutive_timeouts = 0
    
    try: 
        while True:
            current_player_is_p1 = (current_player_name_for_turn == player1_username)

            current_rfile, current_wfile = (player1_rfile, player1_wfile) if current_player_is_p1 else (player2_rfile, player2_wfile)
            other_wfile = player2_wfile if current_player_is_p1 else player1_wfile
            current_board_obj, opponent_board_obj = (player1_board, player2_board) if current_player_is_p1 else (player2_board, player1_board)
            actual_current_player_name = player1_username if current_player_is_p1 else player2_username
            opponent_name = player2_username if current_player_is_p1 else player1_username
            
            send_to_player(current_wfile, f"It's your turn, {actual_current_player_name}!")
            send_to_player(other_wfile, f"Waiting for {actual_current_player_name} to make a move...")
            
            send_board_to_player(current_wfile, current_board_obj, opponent_board_obj)
            
            send_board_to_spectators(player1_board, player2_board, notify_spectators_callback)

            send_to_player(current_wfile, f"Enter coordinate to fire at (e.g. B5): (You have {MOVE_TIMEOUT} seconds)")
            
            guess = recv_from_player_with_timeout(current_rfile, MOVE_TIMEOUT, actual_current_player_name)

            consecutive_timeouts = 0 
            
            if guess.lower() == 'quit':
                send_to_player(current_wfile, "You have quit the game. Your opponent wins by default.")
                send_to_player(other_wfile, f"{actual_current_player_name} has quit. You win by default!")
                notify_spectators_callback(f"{actual_current_player_name} has quit. {opponent_name} wins.")
                return None 

            try:
                row, col = parse_coordinate(guess)
                result, sunk_name = opponent_board_obj.fire_at(row, col)
                
                if result == 'hit':
                    if sunk_name:
                        send_to_player(current_wfile, f"HIT! You sank {opponent_name}'s {sunk_name}!")
                        send_to_player(other_wfile, f"{actual_current_player_name} fired at {guess} and sank your {sunk_name}!")
                        notify_spectators_callback(f"{actual_current_player_name} fired at {guess} and sank {opponent_name}'s {sunk_name}!")
                    else:
                        send_to_player(current_wfile, "HIT!")
                        send_to_player(other_wfile, f"{actual_current_player_name} fired at {guess} and scored a hit!")
                        notify_spectators_callback(f"{actual_current_player_name} fired at {guess} and scored a hit!")

                    send_board_to_player(current_wfile, current_board_obj, opponent_board_obj)
                    send_board_to_player(other_wfile, opponent_board_obj, current_board_obj)
                    send_board_to_spectators(player1_board, player2_board, notify_spectators_callback)

                    if opponent_board_obj.all_ships_sunk():
                        send_to_player(current_wfile, f"Congratulations! You've sunk all of {opponent_name}'s ships. You win!")
                        send_to_player(other_wfile, f"Game over! {actual_current_player_name} has sunk all your ships.")
                        notify_spectators_callback(f"Game over! {actual_current_player_name} has won by sinking all of {opponent_name}'s ships!")
                        return None 
                elif result == 'miss':
                    send_to_player(current_wfile, "MISS!")
                    send_to_player(other_wfile, f"{actual_current_player_name} fired at {guess} and missed!")
                    notify_spectators_callback(f"{actual_current_player_name} fired at {guess} and missed!")

                    send_board_to_player(current_wfile, current_board_obj, opponent_board_obj)
                    send_board_to_player(other_wfile, opponent_board_obj, current_board_obj)
                    send_board_to_spectators(player1_board, player2_board, notify_spectators_callback)
                elif result == 'already_shot':
                    send_to_player(current_wfile, "You've already fired at that location. Try again.")
                    continue  
                elif result == 'invalid':
                    send_to_player(current_wfile, "Invalid coordinate. Please enter a valid coordinate (e.g. A1-J10).")
                    continue 

            except ValueError as e:
                send_to_player(current_wfile, f"Invalid input: {e}. Try again.")
                continue 
            
            current_player_name_for_turn = opponent_name 
            
    except PlayerDisconnectedError as pde:
        turn_for_resumption = actual_current_player_name 
        
        if not pde.game_state: 
            pde.game_state = {
                'player1_board_state': player1_board.serialize(),
                'player2_board_state': player2_board.serialize(),
                'next_turn_username': turn_for_resumption
            }
        else:
            pass
        raise 

    return None


def handle_spectator(conn, addr, spectators):
    """Handles spectator connections."""
    rfile = conn.makefile('r')
    wfile = conn.makefile('w')
    spectators.append((conn, wfile))
    try:
        wfile.write("[INFO] You are now spectating the current game.\n")
        wfile.flush()
        while True:
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

