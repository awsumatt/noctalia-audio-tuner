--!nonstrict
-- [[shortcut]] toggle — control-center quick-toggle tile.
--
-- Mirrors references/example/shortcut.luau:
--   shortcut.setLabel / setIcon / setActive / setEnabled
--   onClick() requests the toggle from the core service via the shared
--   "request" state key (no cross-entry IPC-send API at plugin_api 9).

local active = noctalia.state.get("active_tuning")
local busy = noctalia.state.get("busy") == true
local shortcutSeq = 0

local function render()
  shortcut.setLabel(active ~= nil
    and noctalia.tr("shortcut.on", { tuning = active })
    or noctalia.tr("shortcut.off"))
  shortcut.setIcon("volume", "volume-off")
  shortcut.setActive(active ~= nil)
  shortcut.setEnabled(not busy)
end

-- Initial state at load.
render()

noctalia.state.watch("active_tuning", function(value)
  active = value
  render()
end)

noctalia.state.watch("busy", function(value)
  busy = value == true
  render()
end)

function onClick()
  -- Per-entry thousand-range; core dedupes on a shared monotonic seq.
  shortcutSeq += 1
  noctalia.state.set("request", { seq = 4000 + shortcutSeq, action = "toggle" })
end
