--!nonstrict
-- [[widget]] audio — bar widget for the active tuning.
--
-- Glyph + text from the service's shared state (noctalia.state.watch).
--   click  → toggle / bypass the active tuning (request to the core service)
--   scroll → cycle through candidate tunings (up = previous, down = next)
--
-- UI entries cannot send IPC to the service at plugin_api 9 (no IPC-send
-- API), so requests go through the plugin-shared "request" state key with a
-- monotonic seq; core.luau watches it.

local seq = 1000 -- per-entry thousand-range; core dedupes on a shared seq
local active = nil
local busy = false
local tunings = {}

local function request(action, extra)
  seq += 1
  local req = { seq = seq, action = action }
  for k, v in pairs(extra or {}) do
    req[k] = v
  end
  noctalia.state.set("request", req)
end

local function render()
  if busy then
    barWidget.setGlyph("loader")
    barWidget.setText(noctalia.tr("widget.busy"))
  elseif active ~= nil then
    barWidget.setGlyph("volume")
    barWidget.setText(active)
    barWidget.setColor("primary")
  else
    barWidget.setGlyph("volume-off")
    barWidget.setText(noctalia.tr("widget.bypassed"))
    barWidget.setColor("on_surface")
  end
  local rows = {
    { key = noctalia.tr("widget.state"), value = active or noctalia.tr("widget.bypassed") },
  }
  if #tunings > 0 then
    table.insert(rows, { key = noctalia.tr("widget.candidates"), value = tostring(#tunings) })
  end
  barWidget.setTooltip(rows)
end

noctalia.setUpdateInterval(2000)

noctalia.state.watch("active_tuning", function(value)
  active = value
  render()
end)

noctalia.state.watch("busy", function(value)
  busy = value == true
  render()
end)

noctalia.state.watch("tunings", function(value)
  tunings = type(value) == "table" and value or {}
  render()
end)

function update()
  render()
end

function onClick()
  request("toggle")
end

function onScroll(axis, steps, startsGesture)
  -- Vertical wheel: down (steps > 0) = next candidate, up = previous.
  if axis ~= "vertical" then
    return
  end
  request("cycle", { direction = steps < 0 and "up" or "down" })
end
