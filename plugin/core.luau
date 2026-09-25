--!nonstrict
-- [[service]] core — the backend orchestrator.
--
-- Runs the Python backend (noctalia-audio-tuner) exclusively through the JSON
-- stdout protocol documented in backend/tunerlib/README.md (binding contract):
--
--   * every command is spawned as a /bin/sh -c STRING (API 9: no argv tables);
--     every interpolated value is shell-quoted
--   * the global --progress flag goes BEFORE the subcommand
--   * one-shot commands print exactly one envelope
--     {"status":"ok"|"error","data":...,"error":...}
--   * --progress commands stream NDJSON {"event":"progress",...} lines ending in
--     {"event":"done","data":...}; on error they print the single error envelope
--     and exit 1, so the stream reader must accept BOTH shapes
--   * stdout is parsed ONLY with noctalia.json.decode — never human text
--
-- Command names are the registry names (module names with _ -> -):
--   doctor, devices, probe, capture, analyse, delta, tone, mic-sweep   (SA1)
--   fit, generate, tunings, apply, ab-start, ab-switch, ab-stop        (SA2/SA4)
-- SA2/SA4 commands are called by documented name with documented shapes but
-- degrade gracefully to a clear "not yet available" state when the installed
-- backend does not implement them yet (unknown subcommand => argparse prints
-- usage to stderr and NOTHING to stdout; we classify that as "unimplemented").
--
-- Shared state (noctalia.state) for widget/panel/shortcut/launcher:
--   devices / tunings / active_tuning / busy / progress / generated /
--   apply_result / ab_state / doctor / last_error / backend_ok / sink_prefix /
--   wizard  { stage, session_dir, ... }
--
-- UI entries cannot send IPC to the service at plugin_api 9, so requests arrive
-- through the shared "request" state key with a monotonic seq:
--   refresh | doctor | toggle | cycle | apply | ab | wizard | wizard-candidate

local BACKEND_FALLBACKS = {
  nil, -- filled from config (backend_path)
  "~/.local/bin/noctalia-audio-tuner",
}

local POLL_MS = 30000        -- idle refresh cadence
local CMD_TIMEOUT_MS = 60000 -- runAsync clamps to [50, 60000]

-- Commands whose backend implementation arrives on sibling branches; a
-- missing/unimplemented command is reported as "not yet available", not as a
-- hard error.
local SOFT_COMMANDS = {
  fit = true, generate = true, tunings = true, apply = true,
  ["ab-start"] = true, ["ab-switch"] = true, ["ab-stop"] = true,
}

local backendBin = nil
local sinkPrefix = "noctalia_audio_tuning"
local busy = false
local activeTuning = nil
local lastTuning = nil
local tunings = {}
local reqSeq = 0
local session = nil

-- ── helpers ─────────────────────────────────────────────────────────────────

-- Every interpolated value in a /bin/sh -c string is single-quoted.
local function shQuote(s)
  return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

local function log(msg)
  noctalia.log("[nat-core] " .. msg)
end

local function publish(key, value)
  noctalia.state.set(key, value)
end

local function notify(key, subst, isError)
  local fn = isError and noctalia.notifyError or noctalia.notify
  fn(noctalia.tr("notify.title"), noctalia.tr(key, subst))
end

local function fail(scope, msg)
  log("error (" .. scope .. "): " .. msg)
  busy = false
  publish("busy", false)
  publish("progress", nil)
  publish("last_error", scope .. ": " .. msg)
  noctalia.notifyError(noctalia.tr("notify.error.title"), scope .. ": " .. msg)
end

-- Clear "not yet available" state for SA2/SA4 commands; never a hard failure.
local function unavailable(scope, tried)
  log("not yet available (" .. scope .. "); tried: " .. tostring(tried))
  publish("busy", false)
  publish("progress", nil)
  publish("unavailable", { command = scope, at = os.time() })
  notify("notify.unavailable", { command = scope })
end

-- Resolve the backend binary: configured path first, then the repo install.
local function resolveBackend()
  local candidates = {}
  local configured = noctalia.getConfig("backend_path")
  if configured and configured ~= "" then
    table.insert(candidates, configured)
  end
  for _, p in ipairs(BACKEND_FALLBACKS) do
    table.insert(candidates, p)
  end
  for _, p in ipairs(candidates) do
    local expanded = noctalia.expandPath(p)
    if noctalia.fileExists(expanded) then
      return expanded
    end
  end
  return nil, table.concat(candidates, ", ")
end

-- Parse the backend's single-JSON-object stdout; never trust human text.
-- Returns data, err, unimplemented.
local function parseResponse(result, scope)
  local stdout = result.stdout or ""
  if result.timedOut then
    return nil, "timed out", false
  end
  if (result.stderr or "") ~= "" then
    log(scope .. " stderr: " .. result.stderr)
  end
  local obj = noctalia.json.decode(stdout)
  if obj == nil then
    -- A registered command ALWAYS prints one envelope on stdout; an empty or
    -- non-JSON stdout means the backend does not implement this command yet
    -- (argparse usage error goes to stderr, exit 2, stdout untouched).
    return nil, "stdout is not JSON: " .. stdout:sub(1, 120), true
  end
  if obj.status ~= "ok" then
    local err = (obj.error and obj.error.message) or "unknown backend error"
    return nil, err, false
  end
  return obj.data, nil, false
end

local function buildCmd(bin, args, progress)
  local cmd = shQuote(bin)
  if progress then
    cmd = cmd .. " --progress" -- global flag BEFORE the subcommand
  end
  for _, a in ipairs(args) do
    cmd = cmd .. " " .. shQuote(a)
  end
  return cmd
end

-- One-shot command: runAsync(cmdString, cb, timeoutMs).
-- soft: report unimplemented backend commands via unavailable() instead of fail().
local function runBackend(args, cb, scope, soft)
  if busy then
    cb(nil, "busy with another command")
    return
  end
  local bin, tried = resolveBackend()
  if not bin then
    cb(nil, "backend binary not found (tried " .. tried .. ")")
    return
  end
  local cmd = buildCmd(bin, args, false)
  busy = true
  publish("busy", true)
  publish("last_error", nil)
  log("run: " .. cmd)
  local ok = noctalia.runAsync(cmd, function(result)
    local data, err, unimpl = parseResponse(result, scope)
    busy = false
    publish("busy", false)
    if err then
      if soft and unimpl then
        unavailable(scope, cmd)
        cb(nil, nil)
        return
      end
      fail(scope, err)
      return
    end
    cb(data)
  end, CMD_TIMEOUT_MS)
  if not ok then
    busy = false
    publish("busy", false)
    fail(scope, "runAsync refused to start command")
  end
end

-- Streaming command (--progress): NDJSON progress lines, then {"event":"done"}.
-- ERROR shape: the single error envelope is printed (no done event) and the
-- process exits 1 — both are handled here.
local function runBackendStream(args, onDone, scope, soft)
  if busy then
    onDone(nil, "busy with another command")
    return
  end
  local bin, tried = resolveBackend()
  if not bin then
    onDone(nil, "backend binary not found (tried " .. tried .. ")")
    return
  end
  local cmd = buildCmd(bin, args, true)
  busy = true
  publish("busy", true)
  publish("last_error", nil)
  log("stream: " .. cmd)
  local ok = noctalia.runStream(cmd, function(line)
    local obj = noctalia.json.decode(line)
    if obj == nil then
      log("non-JSON progress line ignored: " .. line:sub(1, 120))
      return
    end
    if obj.event == "progress" then
      publish("progress", { step = scope, pct = obj.pct or 0, msg = obj.msg or "" })
    elseif obj.event == "done" then
      busy = false
      publish("busy", false)
      publish("progress", nil)
      onDone(obj.data, nil)
    elseif obj.status == "error" then
      -- progress-mode error: single envelope, no done event
      busy = false
      publish("busy", false)
      publish("progress", nil)
      local msg = (obj.error and obj.error.message) or "backend error envelope"
      if soft then
        unavailable(scope, cmd)
        onDone(nil, nil)
        return
      end
      fail(scope, msg)
    elseif obj.status == "ok" then
      -- non-progress envelope (backend without --progress support): treat as done
      busy = false
      publish("busy", false)
      publish("progress", nil)
      onDone(obj.data, nil)
    else
      log("unrecognized stream line ignored: " .. line:sub(1, 120))
    end
  end)
  if not ok then
    busy = false
    publish("busy", false)
    fail(scope, "runStream refused to start command")
  end
end

-- ── queries ─────────────────────────────────────────────────────────────────

local function refreshDevices()
  runBackend({ "devices" }, function(data)
    if data ~= nil then
      publish("devices", data)
    end
  end, "devices")
end

local function refreshTunings()
  runBackend({ "tunings" }, function(data)
    tunings = data and data.tunings or {}
    publish("tunings", tunings)
  end, "tunings", true)
end

local function runDoctor()
  runBackend({ "doctor" }, function(data)
    if data == nil then
      publish("backend_ok", false)
      publish("doctor", nil)
      return
    end
    publish("backend_ok", true)
    publish("doctor", data)
  end, "doctor")
end

-- ── apply / toggle / cycle (SA4 shapes; graceful when absent) ───────────────

-- isConfig: true when the id is actually a generated filter-chain path (the
-- wizard apply path uses `apply --config`).
local function applyTuning(id, dryRun, onFinished, isConfig)
  local args
  if isConfig then
    args = { "apply", "--config", id, "--sink-prefix", sinkPrefix }
  else
    args = { "apply", "--tuning", id, "--sink-prefix", sinkPrefix }
  end
  if dryRun then
    table.insert(args, "--dry-run")
  end
  runBackendStream(args, function(data, err)
    if err then
      fail("apply", err)
      return
    end
    if data == nil then
      return -- reported unavailable
    end
    if not dryRun then
      activeTuning = id
      lastTuning = id
      publish("active_tuning", id)
      publish("last_applied", { id = id, at = os.time() })
    end
    publish("apply_result", { id = id, dry_run = dryRun, data = data })
    notify(dryRun and "notify.dry_run_ok" or "notify.applied", { tuning = id })
    if onFinished then
      onFinished(data)
    end
  end, "apply", true)
end

local function toggleTuning()
  if activeTuning ~= nil then
    -- Bypass: stop the A/B chain, remember the tuning for re-apply.
    runBackendStream({ "ab-stop", "--sink-prefix", sinkPrefix }, function(data, err)
      if err then
        fail("ab-stop", err)
        return
      end
      activeTuning = nil
      publish("active_tuning", nil)
      publish("apply_result", { id = lastTuning, bypassed = true, data = data })
    end, "ab-stop", true)
  elseif lastTuning ~= nil then
    applyTuning(lastTuning, false, nil, false)
  else
    fail("toggle", noctalia.tr("error.no_tuning"))
  end
end

local function cycleTuning(dir)
  if #tunings == 0 then
    refreshTunings()
    return
  end
  local idx = 1
  for i, t in ipairs(tunings) do
    local id = type(t) == "table" and (t.id or t.name) or t
    if id == activeTuning then
      idx = i
      break
    end
  end
  idx = ((idx - 1 + dir) % #tunings) + 1
  local t = tunings[idx]
  applyTuning(type(t) == "table" and (t.id or t.name) or t, false, nil, false)
end

-- ── measurement wizard ──────────────────────────────────────────────────────
-- probe → capture (reference) → analyse --out ref.txt
--   [user changes configuration — e.g. applies a candidate tuning]
-- → capture (candidate) → analyse --out raw.txt
-- → delta raw.txt ref.txt → fit → generate
--   [panel offers apply --dry-run, then the confirmed apply]
--
-- Per the contract: analyse writes the response as <freq> <dbfs> text with
-- --out FILE (returning its absolute path under "out"); delta takes
-- RAW.txt REFERENCE.txt positionally. fit/generate are SA2 commands: called by
-- documented name with best-effort shapes (upstream fit consumes the raw
-- response text + reference), and degrade to "not yet available".

local function sessionDir()
  local dir, err = noctalia.pluginDataDir()
  if not dir then
    return nil, err or "pluginDataDir unavailable"
  end
  local path = dir .. "/sessions/" .. os.date("%Y%m%d-%H%M%S")
  local ok, mkErr = noctalia.mkdirAll(path)
  if not ok then
    return nil, mkErr or "mkdirAll failed"
  end
  return path
end

local function wizardPublish(stage, extra)
  local w = extra or {}
  w.stage = stage
  if session ~= nil then
    w.session_dir = session.dir
    w.sink = session.sink
  end
  publish("wizard", w)
end

-- Phase 1: probe → capture ref → analyse --out ref.txt
local function wizardPhaseOne()
  local s = session
  wizardPublish("measuring", { step = "probe" })
  runBackend({ "probe", "--seconds", "6", "--seed", "7", s.dir .. "/dense.wav" }, function(probe)
    if probe == nil then
      wizardPublish("failed", { step = "probe" })
      return
    end
    s.probe = probe
    wizardPublish("measuring", { step = "capture-reference" })
    runBackendStream({
      "capture", "--from", "monitor", "--probe", probe.wav, "--seconds", "2",
      s.sink, s.dir .. "/ref.wav",
    }, function(cap, err)
      if err or cap == nil then
        wizardPublish("failed", { step = "capture-reference" })
        return
      end
      s.capture = cap
      wizardPublish("measuring", { step = "analyse-reference" })
      runBackend({
        "analyse", "--freqs", probe.freqs_path, "--out", s.dir .. "/ref.txt", cap.out,
      }, function(ana)
        if ana == nil then
          wizardPublish("failed", { step = "analyse-reference" })
          return
        end
        s.ref_txt = ana.out or (s.dir .. "/ref.txt")
        -- Reference measured. The user now changes configuration (applies a
        -- candidate tuning) and triggers the candidate capture from the panel.
        wizardPublish("reference_ready", { ref_txt = s.ref_txt })
        notify("notify.wizard_reference_ready")
      end, "analyse")
    end, "capture")
  end, "probe")
end

-- Phase 2: capture raw → analyse --out raw.txt → delta → fit → generate
local function wizardPhaseTwo()
  local s = session
  wizardPublish("measuring", { step = "capture-candidate" })
  runBackendStream({
    "capture", "--from", "monitor", "--probe", s.probe.wav, "--seconds", "2",
    s.sink, s.dir .. "/raw.wav",
  }, function(cap, err)
    if err or cap == nil then
      wizardPublish("failed", { step = "capture-candidate" })
      return
    end
    wizardPublish("measuring", { step = "analyse-candidate" })
    runBackend({
      "analyse", "--freqs", s.probe.freqs_path, "--out", s.dir .. "/raw.txt", cap.out,
    }, function(ana)
      if ana == nil then
        wizardPublish("failed", { step = "analyse-candidate" })
        return
      end
      s.raw_txt = ana.out or (s.dir .. "/raw.txt")
      wizardPublish("measuring", { step = "delta" })
      runBackend({ "delta", s.raw_txt, s.ref_txt }, function(deltaData)
        if deltaData == nil then
          wizardPublish("failed", { step = "delta" })
          return
        end
        s.delta = deltaData
        wizardPublish("measuring", { step = "fit" })
        runBackendStream({ "fit", s.raw_txt, s.ref_txt }, function(fitData, err)
          if err then
            wizardPublish("failed", { step = "fit" })
            return
          end
          if fitData == nil then
            wizardPublish("unavailable", { step = "fit" })
            return
          end
          s.fit = fitData
          local fitPath = fitData.fit or fitData.out or fitData.path
          if fitPath == nil then
            wizardPublish("failed", { step = "generate", msg = "fit returned no fit path" })
            return
          end
          wizardPublish("measuring", { step = "generate" })
          runBackendStream({
            "generate", "--fit", fitPath, "--sink", s.sink,
            "--sink-prefix", sinkPrefix, "--out", s.dir .. "/filter-chain.conf",
          }, function(genData, err)
            if err then
              wizardPublish("failed", { step = "generate" })
              return
            end
            if genData == nil then
              wizardPublish("unavailable", { step = "generate" })
              return
            end
            local conf = genData.out
              or (genData.filter_chain and genData.filter_chain.path)
              or (s.dir .. "/filter-chain.conf")
            publish("generated", { dir = s.dir, config = conf, fit = fitPath })
            wizardPublish("done", { config = conf, fit = fitPath })
            notify("notify.wizard_done")
          end, "generate", true)
        end, "fit", true)
      end, "delta")
    end, "analyse")
  end, "capture")
end

local function startWizard(sink)
  if session ~= nil and session.running then
    fail("wizard", noctalia.tr("error.wizard_running"))
    return
  end
  local dir, err = sessionDir()
  if not dir then
    fail("wizard", err)
    return
  end
  session = { running = true, sink = sink, dir = dir }
  publish("generated", nil)
  wizardPhaseOne()
end

-- ── request routing ─────────────────────────────────────────────────────────

local function handle(action, req)
  if action == "refresh" then
    refreshDevices()
    refreshTunings()
  elseif action == "doctor" then
    runDoctor()
  elseif action == "toggle" then
    toggleTuning()
  elseif action == "cycle" then
    cycleTuning((req and req.direction == "up") and -1 or 1)
  elseif action == "apply" then
    if req == nil or (req.id == nil and req.config == nil) then
      fail("apply", noctalia.tr("error.no_tuning"))
      return
    end
    local isConfig = req.id == nil
    applyTuning(req.id or req.config, req.dry_run == true, nil, isConfig)
  elseif action == "ab" then
    local op = (req and req.op) or "switch"
    if op == "start" then
      local sink = req and req.sink
      if not sink then
        fail("ab-start", noctalia.tr("error.no_sink"))
        return
      end
      runBackendStream({ "ab-start", "--sink", sink, "--sink-prefix", sinkPrefix }, function(data, err)
        if err or data == nil then
          if not err then
            return
          end
          fail("ab-start", err)
          return
        end
        publish("ab_state", { active = true, sink = sink, data = data })
      end, "ab-start", true)
    elseif op == "stop" then
      runBackendStream({ "ab-stop", "--sink-prefix", sinkPrefix }, function(data, err)
        if err or data == nil then
          if not err then
            return
          end
          fail("ab-stop", err)
          return
        end
        publish("ab_state", { active = false, data = data })
      end, "ab-stop", true)
    else
      runBackendStream({ "ab-switch", "--sink-prefix", sinkPrefix }, function(data, err)
        if err or data == nil then
          if not err then
            return
          end
          fail("ab-switch", err)
          return
        end
        publish("ab_state", { active = true, data = data })
      end, "ab-switch", true)
    end
  elseif action == "wizard" then
    local sink = req and req.sink
    if not sink then
      fail("wizard", noctalia.tr("error.no_sink"))
      return
    end
    startWizard(sink)
  elseif action == "wizard-candidate" then
    if session == nil or not session.ref_txt then
      fail("wizard-candidate", noctalia.tr("error.no_reference"))
      return
    end
    wizardPhaseTwo()
  else
    log("unknown request action: " .. tostring(action))
  end
end

function onIpc(event, payload)
  local req = nil
  if payload and payload ~= "" then
    req = noctalia.json.decode(payload)
  end
  handle(event, req)
end

noctalia.state.watch("request", function(req)
  if type(req) ~= "table" or req.seq == nil or req.seq <= reqSeq then
    return
  end
  reqSeq = req.seq
  handle(req.action, req)
end)

function onConfigChanged()
  local p = noctalia.getConfig("sink_prefix")
  if p and p ~= "" then
    sinkPrefix = p
  end
  publish("sink_prefix", sinkPrefix)
  refreshDevices()
  refreshTunings()
end

function onExit(signal, reason)
  -- Detached/running subprocesses are host-owned; just drop our state cleanly.
  log("exit signal=" .. tostring(signal) .. " reason=" .. tostring(reason))
  publish("progress", nil)
  publish("busy", false)
end

-- ── init ────────────────────────────────────────────────────────────────────

sinkPrefix = noctalia.getConfig("sink_prefix") or sinkPrefix
publish("sink_prefix", sinkPrefix)

local bin, tried = resolveBackend()
if bin then
  backendBin = bin
  publish("backend_path_resolved", bin)
  runDoctor()
  refreshDevices()
  refreshTunings()
else
  publish("backend_ok", false)
  publish("last_error", noctalia.tr("error.no_backend") .. " (" .. tried .. ")")
  noctalia.notifyError(noctalia.tr("notify.error.title"), noctalia.tr("error.no_backend"))
end

noctalia.setUpdateInterval(POLL_MS)
function update()
  -- Idle cadence: keep the device list fresh; skip while a command runs.
  if not busy then
    refreshDevices()
  end
end
