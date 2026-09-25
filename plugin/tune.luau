--!nonstrict
-- [[launcher_provider]] tune — quick switcher: devices + tunings.
--
-- Idioms follow references/example/finder.luau:
--   * root level lists two categories (Devices / Tunings); Enter opens one via
--     launcher.setQuery(text) (drill-in, keeps the launcher open in this
--     provider; the host adds our prefix)
--   * queries spawn subprocesses (devices / tunings hit the Python backend),
--     so the manifest sets debounce_ms = 250
--   * a placeholder result is published synchronously in onQuery, then
--     launcher.setResults(query, results) ECHOES the exact query text after
--     the async backend answer
--   * ranking uses noctalia.fuzzyScore
--
-- Requests go to the core service via the shared "request" state key
-- (no cross-entry IPC-send API at plugin_api 9).

local seq = 3000

local function request(action, extra)
  seq += 1
  local req = { seq = seq, action = action }
  for k, v in pairs(extra or {}) do
    req[k] = v
  end
  noctalia.state.set("request", req)
end

-- Last launcher query we answered; re-echoed when refreshed backend data
-- lands asynchronously (placeholder → sync rows → async echo).
local lastQuery = nil

local function placeholder(query, msg)
  launcher.setResults(query, {
    { id = "", title = msg, subtitle = noctalia.tr("tune.subtitle"), glyph = "loader" },
  })
end

-- Root level: category rows (drill-in via launcher.setQuery).
local function rootRows(query)
  return {
    {
      id = "category:devices",
      title = noctalia.tr("tune.category.devices"),
      subtitle = noctalia.tr("tune.category.devices_subtitle"),
      glyph = "volume",
      score = 100,
    },
    {
      id = "category:tunings",
      title = noctalia.tr("tune.category.tunings"),
      subtitle = noctalia.tr("tune.category.tunings_subtitle"),
      glyph = "music",
      score = 99,
    },
  }
end

-- Device rows from the service's `devices` state; sinks first, default sink
-- marked. Activating a sink starts an A/B compare on it (SA4: degrades
-- gracefully when ab-start is not implemented).
local function deviceRows(query)
  local raw = noctalia.state.get("devices")
  local rows = {}
  local n = 0
  local function add(list, kind)
    for _, d in ipairs(list or {}) do
      local title = d.description or d.name or ""
      local score = noctalia.fuzzyScore(query, title)
      if query == "" or score ~= nil then
        n += 1
        rows[n] = {
          id = "sink:" .. (d.name or ""),
          title = title,
          subtitle = noctalia.tr(kind == "sink" and "tune.device.subtitle_sink"
            or "tune.device.subtitle_source")
            .. (d.default and (" · " .. noctalia.tr("tune.default")) or ""),
          glyph = kind == "sink" and "volume" or "mic",
          badge = kind,
          score = score or 0,
        }
      end
    end
  end
  if type(raw) == "table" then
    add(raw.sinks, "sink")
    add(raw.sources, "source")
  end
  if n == 0 then
    rows[1] = { id = "", title = noctalia.tr("tune.no_devices"), glyph = "volume-off", score = 0 }
  end
  return rows
end

-- Tuning rows from the service's `tunings` state. Activating one requests the
-- core service to apply it (SA2: degrades gracefully when apply is missing).
local function tuningRows(query)
  local raw = noctalia.state.get("tunings")
  local active = noctalia.state.get("active_tuning")
  local rows = {}
  local n = 0
  for _, t in ipairs(type(raw) == "table" and raw or {}) do
    local id = type(t) == "table" and (t.id or t.name) or t
    local desc = type(t) == "table" and t.description or nil
    local score = noctalia.fuzzyScore(query, tostring(id))
    if query == "" or score ~= nil then
      n += 1
      rows[n] = {
        id = "tuning:" .. tostring(id),
        title = tostring(id) .. (id == active and (" " .. noctalia.tr("tune.active")) or ""),
        subtitle = desc or noctalia.tr("tune.tuning.subtitle_apply"),
        glyph = "equalizer",
        score = score or 0,
      }
    end
  end
  if n == 0 then
    rows[1] = { id = "", title = noctalia.tr("tune.no_tunings"), glyph = "music-off", score = 0 }
  end
  return rows
end

-- Rank: exact/prefix first, then fuzzy score descending.
local function sortRows(rows, query)
  if query ~= "" then
    table.sort(rows, function(a, b)
      return (a.score or 0) > (b.score or 0)
    end)
  end
  return rows
end

-- Re-echo the last query with current state data (called from the async
-- devices/tunings watch below, after the backend answer lands).
local function republish()
  if lastQuery == nil then
    return
  end
  local text = noctalia.string.trim(lastQuery)
  local view, rest = text:match("^(%S+)%s*(.*)$")
  local rows
  if view == "devices" then
    rows = deviceRows(noctalia.string.trim(rest))
  elseif view == "tunings" then
    rows = tuningRows(noctalia.string.trim(rest))
  else
    rows = rootRows(text)
    if text ~= "" then
      local inline = tuningRows(text)
      for _, r in ipairs(inline) do
        if r.id ~= "" then
          table.insert(rows, r)
        end
      end
    end
  end
  launcher.setResults(lastQuery, sortRows(rows or {}, text))
end

noctalia.state.watch("devices", republish)
noctalia.state.watch("tunings", republish)

-- `query` is the launcher text after this provider's prefix, leading space trimmed.
function onQuery(query)
  lastQuery = query
  local text = noctalia.string.trim(query)
  local view, rest = text:match("^(%S+)%s*(.*)$")
  if view == "devices" or view == "tunings" then
    placeholder(query, noctalia.tr("tune.loading"))
    -- refresh in the background; republish() echoes this query with fresh data
    request("refresh")
    republish()
    return
  end
  -- synchronous placeholder, then republish with the data we already have
  placeholder(query, noctalia.tr("tune.loading"))

  local q = text
  local rows
  if view == nil or (view ~= "devices" and view ~= "tunings") then
    rows = rootRows(q)
    if q ~= "" then
      -- fuzzy-relevant tunings inline at the root
      local inline = tuningRows(q)
      for _, r in ipairs(inline) do
        if r.id ~= "" then
          table.insert(rows, r)
        end
      end
    end
  end

  -- Devices/tunings state may be stale; pull fresh data asynchronously and
  -- re-publish for THIS query (echo the query text exactly).
  request("refresh")
  launcher.setResults(query, sortRows(rows or {}, q))
end

function onActivate(id)
  local category = id:match("^category:(.+)$")
  if category == "devices" then
    launcher.setQuery("devices ") -- host adds our prefix; stays in this provider
    return
  elseif category == "tunings" then
    launcher.setQuery("tunings ")
    return
  end
  local sink = id:match("^sink:(.+)$")
  if sink ~= nil and sink ~= "" then
    request("ab", { op = "start", sink = sink })
    noctalia.notify(noctalia.tr("notify.title"), noctalia.tr("tune.activated_ab", { sink = sink }))
    return
  end
  local tuning = id:match("^tuning:(.+)$")
  if tuning ~= nil and tuning ~= "" then
    request("apply", { id = tuning, dry_run = false })
    noctalia.notify(noctalia.tr("notify.title"), noctalia.tr("tune.activated_apply", { tuning = tuning }))
  end
end
