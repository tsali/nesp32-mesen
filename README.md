# nesp32-mesen

Run the [NESP32](https://github.com/Armitage64/nesp32) cartridge in the
[Mesen](https://github.com/nesdev-org/MesenCE) emulator, with a working,
emulated NESP32 on controller port 2. No hardware needed.

NES Radar, NESradio (with song titles), Weather, Setup and the cartridge menu have
been tested working in MesenCE on Windows, with the bridge on Linux.

This is a response to [nesp32 issue #45](https://github.com/Armitage64/nesp32/issues/45),
"Investigate extending mesen to support nesp32 emulation for developers."

## How it works

The NESP32 cartridge talks to the board over two controller-port wires,
timed to the CPU cycle: OUT0 (bit 0 of `$4016` writes, console to board)
and D0 (bit 0 of `$4017` reads, board to console).

- **`nesp32.lua`** runs in Mesen's script window and is the console's end
  of those wires. It hooks `$4016` writes and `$4017` reads, and uses
  `emu.getCpuCycleCount()` for exact timing. Every change of OUT0 goes to
  the bridge with its cycle. The bridge says which bytes go on D0 and when,
  and every read of `$4017` is answered from that.
- **`bridge/nesp32_bridge.py`** is the board. It runs the NESP32's own
  firmware (its link and services, as nesp32 builds them for a computer:
  `build/libnesp_host`) behind nesp32's own serial line model
  (`tests/wire.py`). It adds real Internet fetches (including ICY stream
  titles), real time, and a Wi-Fi network that is always joined.

The emulated board's clock is the console's CPU cycle count, so the link's
timing is the same as the tests in the nesp32 repo model it.

## Setup

### 1. The bridge

Needs Python 3 and a C compiler (Linux, macOS, or WSL on Windows).

```sh
git clone https://github.com/Armitage64/nesp32.git
cd nesp32 && make host && cd ..
python3 bridge/nesp32_bridge.py --nesp32 ./nesp32
```

It listens on port 7878. Run it on the same computer as Mesen, or on
another one on your network (then set `BRIDGE_HOST` at the top of
`nesp32.lua` to that computer's address).

### 2. Mesen (once)

- **Settings > Input**: set controller **port 2** to **None**. The script is port 2.
- **Settings > Audio**: turn off the option that lowers the volume while
  Mesen is in the background, or NESradio will sound very quiet whenever
  another window has focus.
- **Debug > Script Window**, then the gear (Settings): tick
  **Allow access to I/O and OS functions** and **Allow network access**.
  The script needs Mesen's built-in LuaSocket to reach the bridge.

### 3. Play

1. Load the NESP32 cartridge (`nesp32.nes` from https://nesp32.retrobits.tv/, or one you built).
2. Open `nesp32.lua` in the Script Window and run it. The log should say
   `NESP32: connected to ...`.
3. The apps now get past **WAITING FOR LINK**.

The NESP32's own two buttons are **Insert** (A) and **Delete** (B). Change
`KEY_A` and `KEY_B` at the top of the script to use other keys.

## Known issues

- **Use A, not Turbo A.** In NESradio, A both tunes in and stops a
  station, so Turbo A (Mesen's default keyboard binding is X; plain A is S)
  plays and then stops it one frame later. Tap plain A.
- **Online two-player games** (PEER) are not emulated yet.
- **The Bluetooth keyboard** is not emulated yet.
- **Save states and rewind** are not supported: the board's state lives in
  the bridge, not in Mesen. A reset or a loaded state starts a fresh board.

## Also in progress

A native NESP32 controller type built into Mesen itself (Input > Port 2 >
NESP32), running the same firmware in-process with no script or bridge.

## License

GPL-3.0, the same as nesp32. The bridge loads nesp32's own code.
