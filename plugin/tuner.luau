--!nonstrict
-- [[panel]] tuner — the main surface, a declarative ui.* tree rendered with
-- panel.render() (idioms follow references/example/panel.luau).
--
-- Sections:
--   * Device picker (ui.select over the service's `devices` state — sinks
--     only for capture/AB targets; a warning when the backend reports no
--     internal speaker_sink)
--   * Tunings list (one apply button per tuning from `tunings`)
--   * A/B controls (start / switch / stop, routed through the core service)
--   * Measurement wizard: probe → capture(ref) → analyse(ref.txt) →
--     [user changes config] → capture(raw) → analyse(raw.txt) → delta →
--     fit → generate, with progress bound to the service's `progress` state,
--     then a dry-run confirmation step before the real apply.
--   * "Not yet available" banner when a SA2/SA4 command (fit/generate/tunings/
--     apply/ab-*) is missing from the installed backend.
--
-- All commands go to the core service via the shared "request" state key
-- (no cross-entry IPC-send API at plugin_api 9).

local seq = 2000 -- per-entry thousand-range; core dedupes on a shared seq
local ui = {
  deviceIndex = 0,
  devices = {},        -- flat { {id, description, is_default, kind}, ... }
  deviceOptions = {},
  tunings = {},
  active = nil,
  busy = false,
  progress = nil,      -- { step, pct, msg }
  generated = nil,     -- { dir, config, fit }
  wizard = nil,        -- { stage, session_dir, sink, step, ref_txt, ... }
  applyResult = nil,   -- { id, dry_run, data } | { id, bypassed, data }
  confirmDryRun = false,
  unavailable = nil,   -- { command, at } from the core service
  status = "",
  doctorOk = nil,
  doctor = nil,        -- { deps = {...}, ok, missing }
  backendOk = nil,     -- backend binary found + doctor answered
  devicesRaw = nil,    -- backend devices data (speaker_sink, ...)
}

local function request(action, extra)
  seq += 1
  local req = { seq = seq, action = action }
  for k, v in pairs(extra or {}) do
    req[k] = v
  end
  noctalia.state.set("request", req)
end

local function selectedDevice()
  return ui.devices[ui.deviceIndex + 1]
end

-- ── tree builders ───────────────────────────────────────────────────────────

local function section(title, control)
  return ui.column({ gap = 8 }, {
    ui.label({ text = title, fontWeight = "medium", color = "on_surface_variant" }),
    control,
  })
end

local function doctorSection()
  if ui.doctor == nil then
    if ui.backendOk == false then
      return ui.label({ text = noctalia.tr("panel.backend_missing"), color = "error", maxLines = 3 })
    end
    return nil
  end
  local missingText = nil
  if type(ui.doctor.deps) == "table" then
    local missing = {}
    for _, dep in ipairs(ui.doctor.deps) do
      if not dep.ok then
        table.insert(missing, (dep.name or "?") .. (dep.required and "" or " (optional)"))
      end
    end
    if #missing > 0 then
      missingText = table.concat(missing, ", ")
    end
  end
  if missingText == nil and ui.doctorOk ~= false then
    return nil
  end
  return ui.label({
    text = missingText ~= nil
      and noctalia.tr("panel.doctor_missing", { deps = missingText })
      or noctalia.tr("panel.backend_missing"),
    color = "error",
    maxLines = 3,
  })
end

local function deviceSection()
  local sel = ui.select({
    key = "device-select",
    options = ui.deviceOptions,
    selectedIndex = ui.deviceIndex,
    placeholder = noctalia.tr("panel.no_devices"),
    enabled = not ui.busy and #ui.deviceOptions > 0,
    onChange = "onDeviceChanged",
  })
  local notes = {}
  local doc = doctorSection()
  if doc ~= nil then
    table.insert(notes, doc)
  end
  if type(ui.devicesRaw) == "table" and ui.devicesRaw.speaker_sink == nil then
    table.insert(notes, ui.label({
      text = noctalia.tr("panel.no_speaker_sink"),
      color = "error",
      maxLines = 3,
    }))
  end
  return section(noctalia.tr("panel.section.device"), ui.column({ gap = 8 }, { sel, ui.column({ gap = 4 }, notes) }))
end

local function tuningsSection()
  local rows = {}
  if #ui.tunings == 0 then
    rows[1] = ui.label({ text = noctalia.tr("panel.no_tunings"), color = "on_surface_variant" })
  else
    for i, t in ipairs(ui.tunings) do
      local id = type(t) == "table" and (t.id or t.name) or t
      rows[i] = ui.row({ key = "tuning-" .. tostring(i), gap = 8, align = "center" }, {
        ui.label({ text = id, flexGrow = 1, maxWidth = 300 }),
        ui.button({
          text = noctalia.tr("panel.apply"),
          variant = id == ui.active and "primary" or "outline",
          enabled = not ui.busy,
          onClick = function()
            request("apply", { id = id, dry_run = false })
          end,
        }),
      })
    end
  end
  return section(noctalia.tr("panel.section.tunings"), ui.column({ gap = 4 }, rows))
end

local function abSection()
  local disabled = ui.busy
  return section(noctalia.tr("panel.section.ab"), ui.row({ gap = 8 }, {
    ui.button({ text = noctalia.tr("panel.ab.start"), enabled = not disabled, onClick = "onAbStart" }),
    ui.button({ text = noctalia.tr("panel.ab.switch"), enabled = not disabled, onClick = "onAbSwitch" }),
    ui.button({ text = noctalia.tr("panel.ab.stop"), variant = "destructive", enabled = not disabled, onClick = "onAbStop" }),
    ui.spacer({ flexGrow = 1 }),
    ui.button({
      text = ui.active and noctalia.tr("panel.bypass") or noctalia.tr("panel.reapply"),
      enabled = not disabled,
      onClick = "onToggle",
    }),
  }))
end

-- Progress row bound to the service's `progress` state; visible only while a
-- streamed command (capture/fit/generate/apply) is running.
local function progressSection()
  if ui.progress == nil and not ui.busy then
    return nil
  end
  local pct = ui.progress and ui.progress.pct
  local fraction = (type(pct) == "number") and (pct / 100) or nil
  local msg = ui.progress and (ui.progress.step .. (ui.progress.msg ~= "" and (" — " .. ui.progress.msg) or ""))
    or noctalia.tr("panel.working")
  return section(noctalia.tr("panel.section.progress"), ui.column({ gap = 6 }, {
    ui.label({ text = msg, color = "on_surface_variant" }),
    ui.progress({ progress = fraction, radius = 4 }),
  }))
end

-- Wizard section, driven by the service's `wizard` state stages:
--   measuring → reference_ready → measuring → done | failed | unavailable
local function wizardSection()
  local dev = selectedDevice()
  local stage = ui.wizard and ui.wizard.stage
  local rows = {}
  rows[1] = ui.button({
    text = noctalia.tr("panel.wizard.start"),
    variant = "primary",
    enabled = not ui.busy and dev ~= nil,
    onClick = "onWizardStart",
  })
  if dev ~= nil then
    rows[2] = ui.label({
      text = noctalia.tr("panel.wizard.target", { device = dev.description or dev.id }),
      color = "on_surface_variant",
    })
  end

  if stage == "reference_ready" then
    table.insert(rows, ui.separator({ spacing = 8 }))
    table.insert(rows, ui.label({ text = noctalia.tr("panel.wizard.reference_ready"), color = "on_surface" }))
    table.insert(rows, ui.button({
      text = noctalia.tr("panel.wizard.capture_candidate"),
      enabled = not ui.busy,
      onClick = "onWizardCandidate",
    }))
  elseif stage == "measuring" then
    table.insert(rows, ui.label({
      text = noctalia.tr("panel.wizard.step", { step = ui.wizard.step or "" }),
      color = "on_surface_variant",
    }))
  elseif stage == "failed" then
    table.insert(rows, ui.label({
      text = noctalia.tr("panel.wizard.failed", { step = ui.wizard.step or "" }),
      color = "error",
    }))
  end

  if ui.generated ~= nil then
    table.insert(rows, ui.separator({ spacing = 8 }))
    table.insert(rows, ui.label({
      text = noctalia.tr("panel.wizard.generated", { path = ui.generated.config }),
      color = "on_surface",
    }))
    if ui.confirmDryRun then
      -- Dry-run confirmation step: show the dry-run report, then offer apply.
      local report = ui.applyResult and ui.applyResult.data
      local reportText = noctalia.tr("panel.wizard.dry_run_pending")
      if report ~= nil then
        local encoded = noctalia.json.encode(report)
        reportText = type(encoded) == "string" and encoded:sub(1, 400) or noctalia.tr("panel.wizard.dry_run_ok")
      elseif ui.applyResult ~= nil and ui.applyResult.dry_run then
        reportText = noctalia.tr("panel.wizard.dry_run_ok")
      end
      table.insert(rows, ui.label({ text = reportText, color = "on_surface_variant", maxLines = 6 }))
      table.insert(rows, ui.row({ gap = 8 }, {
        ui.button({
          text = noctalia.tr("panel.wizard.apply"),
          variant = "primary",
          enabled = not ui.busy,
          onClick = "onWizardApply",
        }),
        ui.button({ text = noctalia.tr("panel.wizard.cancel"), onClick = "onWizardCancel" }),
      }))
    else
      table.insert(rows, ui.button({
        text = noctalia.tr("panel.wizard.dry_run"),
        enabled = not ui.busy,
        onClick = "onWizardDryRun",
      }))
    end
  end
  return section(noctalia.tr("panel.section.wizard"), ui.column({ gap = 8 }, rows))
end

local function unavailableSection()
  if ui.unavailable == nil then
    return nil
  end
  return ui.label({
    text = noctalia.tr("panel.unavailable", { command = ui.unavailable.command or "" }),
    color = "error",
    maxLines = 3,
  })
end

local function render()
  local header = ui.row({ align = "center", justify = "space_between", gap = 8 }, {
    ui.label({
      text = noctalia.tr("panel.title") .. (ui.active and (" — " .. ui.active) or ""),
      fontSize = 16, fontWeight = "bold", color = "on_surface", flexGrow = 1,
    }),
    ui.button({ glyph = "refresh", enabled = not ui.busy, onClick = "onRefresh" }),
    ui.button({ glyph = "close", onClick = "onCloseClicked" }),
  })

  local body = {}
  body[1] = deviceSection()
  body[2] = tuningsSection()
  body[3] = abSection()
  body[4] = wizardSection()
  local un = unavailableSection()
  if un ~= nil then
    table.insert(body, un)
  end
  local prog = progressSection()
  if prog ~= nil then
    table.insert(body, prog)
  end
  if ui.status ~= "" then
    table.insert(body, ui.label({ text = ui.status, color = "error", maxLines = 4 }))
  end

  panel.render(ui.column({ flexGrow = 1, gap = 16 }, {
    header,
    ui.scroll({ flexGrow = 1, gap = 16 }, body),
  }))
end

-- ── state wiring ────────────────────────────────────────────────────────────

local function flattenDevices(raw)
  -- `devices` data shape (backend contract): sinks for capture/AB targets,
  -- sources listed for reference. Accept a flat list defensively.
  local flat = {}
  local function add(list, kind)
    for _, d in ipairs(list or {}) do
      table.insert(flat, {
        id = d.name or d.id,
        description = d.description or d.name or d.id,
        is_default = d.default or false,
        kind = kind,
      })
    end
  end
  if type(raw) == "table" and raw.sinks ~= nil then
    add(raw.sinks, "sink")
    add(raw.sources, "source")
  elseif type(raw) == "table" then
    add(raw, "sink")
  end
  return flat
end

local function syncFromState()
  -- Keep the device select's controlled index aligned with new data.
  local prevId = ui.devices[ui.deviceIndex + 1]
  local fresh = {}
  for key, val in pairs({
    devices = noctalia.state.get("devices"),
    tunings = noctalia.state.get("tunings"),
    active = noctalia.state.get("active_tuning"),
    busy = noctalia.state.get("busy"),
    progress = noctalia.state.get("progress"),
    generated = noctalia.state.get("generated"),
    applyResult = noctalia.state.get("apply_result"),
    doctor = noctalia.state.get("doctor"),
    wizard = noctalia.state.get("wizard"),
    unavailable = noctalia.state.get("unavailable"),
    backendOk = noctalia.state.get("backend_ok"),
  }) do
    fresh[key] = val
  end
  ui.devicesRaw = fresh.devices
  ui.devices = flattenDevices(fresh.devices)
  ui.deviceOptions = {}
  for i, d in ipairs(ui.devices) do
    local label = d.description or d.id
    if d.kind == "source" then
      label = label .. " " .. noctalia.tr("panel.source_suffix")
    end
    ui.deviceOptions[i] = (d.is_default and "★ " or "") .. label
    if prevId ~= nil and d.id == prevId.id then
      ui.deviceIndex = i - 1
    end
  end
  ui.tunings = type(fresh.tunings) == "table" and fresh.tunings or {}
  ui.active = fresh.active
  ui.busy = fresh.busy == true
  ui.progress = fresh.progress
  ui.generated = fresh.generated
  ui.applyResult = fresh.applyResult
  ui.wizard = type(fresh.wizard) == "table" and fresh.wizard or nil
  ui.unavailable = type(fresh.unavailable) == "table" and fresh.unavailable or nil
  ui.doctor = type(fresh.doctor) == "table" and fresh.doctor or nil
  ui.doctorOk = ui.doctor and ui.doctor.ok or nil
  ui.backendOk = fresh.backendOk
  render()
end

noctalia.state.watch("devices", syncFromState)
noctalia.state.watch("tunings", syncFromState)
noctalia.state.watch("active_tuning", syncFromState)
noctalia.state.watch("busy", syncFromState)
noctalia.state.watch("progress", syncFromState)
noctalia.state.watch("generated", syncFromState)
noctalia.state.watch("apply_result", syncFromState)
noctalia.state.watch("doctor", syncFromState)
noctalia.state.watch("backend_ok", syncFromState)
noctalia.state.watch("wizard", syncFromState)
noctalia.state.watch("unavailable", syncFromState)

-- ── callbacks (globals the host resolves by name) ───────────────────────────

function onOpen(_context)
  request("refresh")
  syncFromState()
end

function onClose()
  -- Nothing to release; state lives in the service.
end

function onRefresh()
  request("refresh")
end

function onCloseClicked()
  panel.close()
end

function onDeviceChanged(index, _text)
  ui.deviceIndex = tonumber(index) or 0
  render()
end

function onToggle()
  request("toggle")
end

function onAbStart()
  local dev = selectedDevice()
  if dev == nil or dev.kind ~= "sink" then
    ui.status = noctalia.tr("error.no_sink")
    render()
    return
  end
  request("ab", { op = "start", sink = dev.id })
end

function onAbSwitch()
  request("ab", { op = "switch" })
end

function onAbStop()
  request("ab", { op = "stop" })
end

function onWizardStart()
  local dev = selectedDevice()
  if dev == nil or dev.kind ~= "sink" then
    ui.status = noctalia.tr("error.no_sink")
    render()
    return
  end
  ui.confirmDryRun = false
  request("wizard", { sink = dev.id })
end

function onWizardCandidate()
  request("wizard-candidate")
end

function onWizardDryRun()
  -- Dry run is forced until the user confirms; this is the confirmation step.
  local cfg = ui.generated and ui.generated.config
  if cfg == nil then
    return
  end
  request("apply", { config = cfg, dry_run = true })
  ui.confirmDryRun = true
end

function onWizardApply()
  -- User confirmed the dry-run report: apply for real (backend still backs up
  -- and restores on failure, per AGENTS.md hard rule 5).
  local cfg = ui.generated and ui.generated.config
  if cfg == nil then
    return
  end
  request("apply", { config = cfg, dry_run = false })
  ui.confirmDryRun = false
end

function onWizardCancel()
  ui.confirmDryRun = false
  render()
end

function onConfigChanged()
  syncFromState()
end
