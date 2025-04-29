def run_two_player_game(player1_rfile, player1_wfile, player2_rfile, player2_wfile, broadcast_to_spectators=None):
    """
    Run a two-player game of Battleship.
    Returns the final board states for both players.
    """
    # Initialize boards
    board1 = Board()
    board2 = Board()
    
    # Place ships for both players
    player1_wfile.write("\nPlayer 1, place your ships:\n")
    player1_wfile.flush()
    place_ships(player1_rfile, player1_wfile, board1)
    
    player2_wfile.write("\nPlayer 2, place your ships:\n")
    player2_wfile.flush()
    place_ships(player2_rfile, player2_wfile, board2)
    
    # Game loop
    current_player = 1
    while True:
        if current_player == 1:
            attacker_rfile = player1_rfile
            attacker_wfile = player1_wfile
            defender_board = board2
            attacker_board = board1
            attacker_name = "Player 1"
            defender_name = "Player 2"
        else:
            attacker_rfile = player2_rfile
            attacker_wfile = player2_wfile
            defender_board = board1
            attacker_board = board2
            attacker_name = "Player 2"
            defender_name = "Player 1"
        
        # Display boards
        attacker_wfile.write("\nYour grid:\n")
        attacker_wfile.write(str(attacker_board))
        attacker_wfile.write("\nOpponent's grid:\n")
        attacker_wfile.write(str(defender_board))
        attacker_wfile.flush()
        
        # Get attack coordinates
        attacker_wfile.write(f"\n{attacker_name}, enter coordinates to attack (e.g., A1): ")
        attacker_wfile.flush()
        
        try:
            coords = attacker_rfile.readline().strip().upper()
            if not coords:
                raise ValueError("Empty input")
            
            # Parse coordinates
            if len(coords) < 2:
                raise ValueError("Invalid coordinates")
            
            col = ord(coords[0]) - ord('A')
            row = int(coords[1:]) - 1
            
            if not (0 <= row < defender_board.size and 0 <= col < defender_board.size):
                raise ValueError("Coordinates out of bounds")
            
            # Check if already attacked
            if defender_board.display_grid[row][col] != '~':
                raise ValueError("Already attacked this position")
            
            # Make the attack
            result = defender_board.attack(row, col)
            
            # Update display
            if result == 'H':
                defender_board.display_grid[row][col] = 'X'
                attacker_wfile.write(f"\n{attacker_name} hit a ship at {coords}!\n")
                if broadcast_to_spectators:
                    broadcast_to_spectators(f"{attacker_name} hit a ship at {coords}!")
            else:
                defender_board.display_grid[row][col] = 'O'
                attacker_wfile.write(f"\n{attacker_name} missed at {coords}.\n")
                if broadcast_to_spectators:
                    broadcast_to_spectators(f"{attacker_name} missed at {coords}.")
            
            # Check if game is over
            if defender_board.all_ships_sunk():
                attacker_wfile.write(f"\n{attacker_name} wins! All of {defender_name}'s ships have been sunk!\n")
                if broadcast_to_spectators:
                    broadcast_to_spectators(f"{attacker_name} wins! All of {defender_name}'s ships have been sunk!")
                return board1, board2
            
            # Switch players
            current_player = 2 if current_player == 1 else 1
            
        except ValueError as e:
            attacker_wfile.write(f"\nError: {str(e)}\n")
            attacker_wfile.flush()
        except Exception as e:
            attacker_wfile.write(f"\nUnexpected error: {str(e)}\n")
            attacker_wfile.flush()
            raise

def place_ships(rfile, wfile, board):
    """
    Place ships on the board.
    """
    ships = [
        ("Carrier", 5),
        ("Battleship", 4),
        ("Cruiser", 3),
        ("Submarine", 3),
        ("Destroyer", 2)
    ]
    
    for ship_name, ship_length in ships:
        while True:
            try:
                wfile.write(f"\nPlace your {ship_name} (length {ship_length})\n")
                wfile.write("Enter starting coordinates and direction (e.g., A1 H for horizontal): ")
                wfile.flush()
                
                input_str = rfile.readline().strip().upper()
                if not input_str:
                    raise ValueError("Empty input")
                
                # Parse input
                parts = input_str.split()
                if len(parts) != 2:
                    raise ValueError("Invalid input format")
                
                coords = parts[0]
                direction = parts[1]
                
                if len(coords) < 2:
                    raise ValueError("Invalid coordinates")
                
                col = ord(coords[0]) - ord('A')
                row = int(coords[1:]) - 1
                
                if not (0 <= row < board.size and 0 <= col < board.size):
                    raise ValueError("Coordinates out of bounds")
                
                if direction not in ['H', 'V']:
                    raise ValueError("Direction must be H (horizontal) or V (vertical)")
                
                # Check if ship can be placed
                if direction == 'H':
                    if col + ship_length > board.size:
                        raise ValueError("Ship would go out of bounds")
                    for c in range(col, col + ship_length):
                        if board.grid[row][c] != '~':
                            raise ValueError("Ship would overlap with another ship")
                else:  # Vertical
                    if row + ship_length > board.size:
                        raise ValueError("Ship would go out of bounds")
                    for r in range(row, row + ship_length):
                        if board.grid[r][col] != '~':
                            raise ValueError("Ship would overlap with another ship")
                
                # Place the ship
                if direction == 'H':
                    for c in range(col, col + ship_length):
                        board.grid[row][c] = 'S'
                else:
                    for r in range(row, row + ship_length):
                        board.grid[r][col] = 'S'
                
                # Update display
                wfile.write("\nYour grid:\n")
                wfile.write(str(board))
                wfile.flush()
                break
                
            except ValueError as e:
                wfile.write(f"\nError: {str(e)}\n")
                wfile.flush()
            except Exception as e:
                wfile.write(f"\nUnexpected error: {str(e)}\n")
                wfile.flush()
                raise 