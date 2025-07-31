# CITS3003 (2025) Project: Networked Battleships Game

Student: Charles Johnson
Grade: 98%

## Battleship Game

A networked implementation of the classic Battleship game for CITS3002.

### How to Play

1. Start the server:

```bash
python server.py
```

2. Start one or more clients:

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

## Project Structure

- `server.py`: Main server implementation
- `client.py`: Client/GUI implementation
- `battleship.py`: Core game logic
- `protocol.py`: Network protocol implementation
- `protocol_test.py`: Test file for protocol implementation
