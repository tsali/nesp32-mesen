-- nesp32.lua: an NESP32 on controller port 2, for Mesen.
--
-- The NESP32 (github.com/Armitage64/nesp32) is a Wi-Fi board on the NES
-- controller port. Its cartridge talks to it over a serial line made of two
-- controller-port wires: OUT0 ($4016 bit 0, console -> NESP32) and D0
-- ($4017 bit 0, NESP32 -> console), timed to the CPU cycle.
--
-- This script is the console's end of those wires. The NESP32 itself (its
-- real firmware, and the Internet) is nesp32_bridge.py, which this connects
-- to. Every change of OUT0 is sent to the bridge with its cycle; the bridge
-- says which bytes go on D0 and when, and every read of $4017 is answered
-- from that.
--
-- Set up, once:
--   1. Settings > Input: controller port 2 = None (this script is port 2).
--   2. Debug > Script Window > Settings (the gear): tick
--      "Allow access to I/O and OS functions" and "Allow network access".
-- Then load the NESP32 cartridge, open this script in the Script Window,
-- and run it.

local BRIDGE_HOST = "127.0.0.1"       -- the computer running nesp32_bridge.py (this one, by default)
local BRIDGE_PORT = 7878
local KEY_A = "Insert"                 -- the NESP32's own two buttons
local KEY_B = "Delete"

local socket = require("socket.core")

-- The CPU's cycle count. emu.getCpuCycleCount is in MesenCE and recent
-- Mesen 2 builds; older ones only have it in emu.getState(), which is far
-- slower (a copy of the whole state each call), so the radio may stutter there.
local cycles
if emu.getCpuCycleCount then
  cycles = emu.getCpuCycleCount
else
  cycles = function() return emu.getState()["cpu.cycleCount"] end
end

local conn = nil
local changes = {}        -- OUT0 changes since the last sync: cycle, level, cycle, level...
local lastOut = 0
local busy = false        -- a byte from the console is partway into the bridge
local runs = {}           -- bytes on D0: { s = start, p = period, f = frame length, n = count, b = { bytes } }
local runFirst, runLast = 1, 0
local lastCycle = 0
local status = "connecting"
local keyWas = { false, false }

local retryIn = 0

local function connect()
  local c = socket.tcp()
  c:settimeout(0.3)
  local ok, err = c:connect(BRIDGE_HOST, BRIDGE_PORT)
  if not ok then
    status = "no bridge at " .. BRIDGE_HOST .. ":" .. BRIDGE_PORT .. " (" .. tostring(err) .. ")"
    emu.log("NESP32: " .. status)
    return nil
  end
  c:setoption("tcp-nodelay", true)
  c:settimeout(2)
  -- A fresh board: what the console did while there was none is of no use
  -- to it, and the line starts quiet.
  changes, runs, runFirst, runLast = {}, {}, 1, 0
  busy = false
  status = "connected"
  emu.log("NESP32: connected to " .. BRIDGE_HOST .. ":" .. BRIDGE_PORT)
  return c
end

local function addRuns(words, i, count)
  for _ = 1, count do
    local s, p, hex = tonumber(words[i]), tonumber(words[i + 1]), words[i + 2]
    i = i + 3
    local bytes = {}
    for k = 1, #hex, 2 do
      bytes[#bytes + 1] = tonumber(string.sub(hex, k, k + 1), 16)
    end
    runLast = runLast + 1
    runs[runLast] = { s = s, p = p, f = p * 11, n = #bytes, b = bytes, e = s + p * 11 * #bytes }
  end
end

-- Tell the bridge the time and OUT0's changes; take the bytes it puts on D0.
local function sync(cycle)
  if conn == nil then
    return
  end
  local parts = { "S", tostring(math.floor(cycle)), tostring(#changes // 2) }
  for k = 1, #changes do
    parts[#parts + 1] = tostring(math.floor(changes[k]))
  end
  changes = {}
  local ok, err = conn:send(table.concat(parts, " ") .. "\n")
  if not ok then
    status = "bridge lost (" .. tostring(err) .. ")"
    conn = nil
    return
  end
  local line, rerr = conn:receive("*l")
  if not line then
    status = "bridge lost (" .. tostring(rerr) .. ")"
    conn = nil
    return
  end
  local words = {}
  for w in string.gmatch(line, "%S+") do
    words[#words + 1] = w
  end
  busy = words[2] == "1"
  addRuns(words, 4, tonumber(words[3]))
end

-- The level of D0 at a cycle: mark (1) when nothing is being sent.
local function level(cycle)
  while runFirst <= runLast and cycle >= runs[runFirst].e do
    runs[runFirst] = nil
    runFirst = runFirst + 1
  end
  local run = runs[runFirst]
  if run == nil or cycle < run.s then
    return 1
  end
  local rel = cycle - run.s
  local index = rel // run.f
  local bit = (rel - index * run.f) / run.p
  if bit < 1 then
    return 0                                    -- start bit
  elseif bit < 9 then
    return (run.b[index + 1] >> math.floor(bit - 1)) & 1
  end
  return 1                                      -- stop bits
end

local function resetLine(cycle)
  changes, runs, runFirst, runLast = {}, {}, 1, 0
  busy = false
  lastCycle = cycle
end

-- $4016 write: OUT0 is bit 0
local function onWrite(address, value)
  local cycle = cycles()
  local out = value & 1
  if out ~= lastOut then
    lastOut = out
    changes[#changes + 1] = cycle
    changes[#changes + 1] = out
  end
end

-- $4017 read: D0, inverted by the console's buffer, in bit 0
local function onRead(address, value)
  local cycle = cycles()
  if cycle < lastCycle then
    resetLine(cycle)
  end
  lastCycle = cycle
  if #changes > 0 or busy then
    sync(cycle)
  end
  return (value & 0xFE) | (level(cycle) ~ 1)
end

local function onFrame()
  local cycle = cycles()
  if conn == nil then
    retryIn = retryIn - 1
    if retryIn <= 0 then
      retryIn = 120                             -- try again every two seconds
      conn = connect()
    end
  end
  if cycle < lastCycle then
    resetLine(cycle)
  end
  lastCycle = cycle
  for i, key in ipairs({ KEY_A, KEY_B }) do
    local down = emu.isKeyPressed(key)
    if down and not keyWas[i] and conn ~= nil then
      conn:send("B " .. i .. "\n")
    end
    keyWas[i] = down
  end
  sync(cycle)
  if conn == nil then
    emu.drawString(4, 4, "NESP32: " .. status, 0xFFFFFF, 0x80000000)
  end
end

conn = connect()
emu.addMemoryCallback(onWrite, emu.callbackType.write, 0x4016)
emu.addMemoryCallback(onRead, emu.callbackType.read, 0x4017)
emu.addEventCallback(onFrame, emu.eventType.endFrame)
emu.displayMessage("NESP32", status)
