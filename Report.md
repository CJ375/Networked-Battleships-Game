<h1 style="text-align: center;">CITS3002 Project Report</h1>

### Members
1. Charles Johnson (22236068) - Solo

### Task Completion

# Implementations

### Program Assumptions:
1. Clients will always send valid commands in sequence.

## Tier 1

### T1.1: Concurrency Issues - Message Synchronization

**Problem:** The client used a sequential approach for I/O operations where it read server messages, waited for user input, sent input, then read the next message. This failed when the server needed to send multiple messages before expecting input, causing the client to be perpetually behind.

**Solution:** Implemented a threaded approach that separates receiving and sending operations:
- Created a dedicated receiver thread that continuously reads and displays server messages
- Kept the main thread focused on handling user input and sending commands
- Used a global flag for thread-safe communication

### T1.2: Server and Two Clients

**Implementation:** Created a server that accepts exactly two player connections to start a game:
- Used socket programming to create a listening server on a configurable port
- Implemented a connection handler to accept and validate client connections
- Stored player connections in memory for the duration of the game
- Started game sessions in their own threads once both players connected

### T1.3: Basic Game Flow

**Implementation:** Adapted the single-player battleship logic from the provided `battleship.py` script to support two players:
- Ship placement phase where each player places 5 ships (manually or randomly)
- Turn-based gameplay where players alternate firing at coordinates
- Tracking of hits, misses, and sunken ships with appropriate notifications
- Game termination when all ships of a player are sunk
- Designed a `run_two_player_game` function that manages the core gameplay loop

### T1.4: Simple Client/Server Message Exchange

**Design:** Implemented a straightforward text-based protocol for communication:
- Ship placement commands: `PLACE A1 H BATTLESHIP` (coordinate, orientation, ship type)
- Firing commands: e.g., `B5` (target coordinate)
- Result messages: `HIT`, `MISS`, `SUNK DESTROYER`, etc.
- Board updates for displaying the current game state
- End game notifications with results

The client and server parse these messages to update game state and respond appropriately.

### T1.5: No Disconnection Handling (Initial State)

**Approach:** For the initial implementation, connections were assumed to be stable:
- No explicit handling of unexpected disconnections
- Game would error if a player disconnected mid-game
- Server would close all connections when a game ended