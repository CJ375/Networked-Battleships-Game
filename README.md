# CITS3002 Project 2025

Student: Charles Johnson - 22236068

Tasks:

1. ***Tier 1: Basic 2-Player Game with Concurrency***
    1. T1.1: Concurrency Issues - ***Done***
    2. T1.2: Server and Two Clients - ***Done***
    3. T1.3: Basic Game Flow - ***Done***
    4. T1.4: Simple Client/Server Message Exchange - ***Done***
    5. T1.5: No Disconnection Handling - ***Done***
2. ***Tier 2: Gameplay Quality-of-Life & Scalability***
    1. T2.1: Extended Input Validation - ***Done***
    2. T2.2: Support Multiple Games - ***Done***
    3. T2.3: Timeout Handling - ***Done***
    4. T2.4: Disconnection Handling - ***Done***
    5. T2.5: Communication with Idle or Extra Clients - ***Done***

## Battleship Game

A networked implementation of the classic Battleship game for CITS3002.

### Features

- Client-server architecture for playing Battleship over a network
- Multi-threaded server to handle multiple clients simultaneously
- Support for spectators to watch ongoing games
- Custom network protocol implementation with integrity checking
- Automatic and manual ship placement options
- Multiple games can be played in succession
- Player reconnection support with a 60-second window
- Reliable packet delivery with corruption detection and retransmission
- Checksum support
- Disconnection handling and timeout management

### How to Play

1. Start the server:

```bash
python server.py
```

2. Start a client:

```bash
python client.py
```

3. Enter a username when prompted

4. Either:

   - Join a game immediately if another player is waiting
   - Be placed in a waiting queue
   - Become a spectator if a game is already in progress

5. Follow the prompts to place your ships and take turns firing

6. After a game ends, you'll be asked if you want to play again

### Reconnection Support

If you're disconnected during a game, you have 60 seconds to reconnect:

1. Simply restart the client and enter the same username
2. The server will automatically recognize you're reconnecting
3. You'll be placed back into your ongoing game
4. If you don't reconnect within 60 seconds, your opponent wins by default

### Protocol Information

The game uses a custom network protocol with:

- 17-byte header (magic number, sequence number, packet type, data length, checksum)
- CRC32 checksums for data integrity
- Packet retransmission for reliability
- Various packet types for different game actions

## Testing

- `test_protocol.py`: Unit tests for protocol implementation
- `test_network.py`: Tests for network functionality
- `test_reconnect.py`: Test for reconnection functionality

Run the tests with:

```bash
python3 -m unittest test_protocol.py
python3 -m unittest test_network.py
python3 test_reconnect.py
```

## Project Structure

- `server.py`: Main server implementation
- `client.py`: Client implementation
- `battleship.py`: Core game logic
- `protocol.py`: Network protocol implementation
- `test_*.py`: Test files
