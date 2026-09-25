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
local busyScope = nil
local watchdogTick = 0 -- advanced in update() (one tick per update cadence)
local lastStreamTick = nil -- tick of the last stream line; nil = not streaming
local streamDead = false -- watchdog fired; late lines from the dead stream ignored
-- runStream has no completion callback at API 9: a child that dies without a
-- done event or an error envelope would leave busy set forever. update()'s
-- watchdog recovers after STREAM_WATCHDOG_TICKS updates with no line at all
-- (~5 min at the 30 s cadence; every stream line resets the counter).
local STREAM_WATCHDOG_TICKS = 10
local devices = nil
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
  busyScope = nil
  lastStreamTick = nil
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
  busyScope = scope
  lastStreamTick = watchdogTick
  streamDead = false
  publish("busy", true)
  publish("last_error", nil)
  log("stream: " .. cmd)
  local ok = noctalia.runStream(cmd, function(line)
    if streamDead then
      return -- late line from a stream the watchdog already declared dead
    end
    lastStreamTick = watchdogTick
    local obj = noctalia.json.decode(line)
    if obj == nil then
      log("non-JSON progress line ignored: " .. line:sub(1, 120))
      return
    end
    if obj.event == "progress" then
      publish("progress", { step = scope, pct = obj.pct or 0, msg = obj.msg or "" })
    elseif obj.event == "done" then
      busy = false
      busyScope = nil
      lastStreamTick = nil
      publish("busy", false)
      publish("progress", nil)
      onDone(obj.data, nil)
    elseif obj.status == "error" then
      -- progress-mode error: single envelope, no done event
      busy = false
      busyScope = nil
      lastStreamTick = nil
      publish("busy", false)
      publish("progress", nil)
      local msg = (obj.error and obj.error.message) or "backend error envelope"
      if soft then
        -- A MISSING command produces silence (argparse usage on stderr, no
        -- stdout), so an error envelope in soft mode is always a real
        -- runtime failure of a live command (e.g. "no active A/B session").
        -- Hand it to the caller; genuinely absent commands hit the watchdog.
        onDone(nil, msg)
        return
      end
      fail(scope, msg)
    elseif obj.status == "ok" then
      -- non-progress envelope (backend without --progress support): treat as done
      busy = false
      busyScope = nil
      lastStreamTick = nil
      publish("busy", false)
      publish("progress", nil)
      onDone(obj.data, nil)
    else
      log("unrecognized stream line ignored: " .. line:sub(1, 120))
    end
  end)
  if not ok then
    busy = false
    busyScope = nil
    lastStreamTick = nil
    publish("busy", false)
    fail(scope, "runStream refused to start command")
  end
end

-- ── queries ─────────────────────────────────────────────────────────────────

local function refreshDevices()
  runBackend({ "devices" }, function(data)
    if data ~= nil then
      devices = data
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

-- Reconciled to the landed SA4 contracts: `apply CONFIG [--sink S] [--name N]
-- [--write]` — dry-run IS the default (there is no --dry-run flag); installing
-- does NOT reload PipeWire and does NOT switch sinks by itself. Switching the
-- active sink happens through the A/B session (`ab-start --prefix P`,
-- `ab-switch [N | --name S]`, `ab-stop [--keep]`), which owns landing
-- verification and full restore.

local function tuningSinkName(t)
  if type(t) == "table" then
    return t.prefix or t.path
  end
  return t
end

-- Install a generated conf (wizard output). realInstall=false -> backend
-- default dry-run (no --write); the confirmed panel step re-calls with
-- realInstall=true -> --write + --sink for @SPEAKER_SINK@ substitution.
local function installConfig(confPath, realInstall)
  local sink = (session and session.sink)
    or (devices and devices.speaker_sink)
  if sink == nil then
    fail("apply", "no target sink known — run the wizard or pick a sink")
    return
  end
  local args = { "apply", confPath, "--sink", sink, "--name", sinkPrefix }
  if realInstall then
    table.insert(args, "--write")
  end
  runBackendStream(args, function(data, err)
    if err then
      fail("apply", err)
      return
    end
    if data == nil then
      return -- reported unavailable
    end
    if realInstall then
      activeTuning = confPath
      lastTuning = confPath
      publish("active_tuning", confPath)
      publish("last_applied", { id = confPath, at = os.time() })
    end
    publish("apply_result", { id = confPath, dry_run = not realInstall, data = data })
    notify(realInstall and "notify.applied" or "notify.dry_run_ok",
           { tuning = confPath })
  end, "apply", true)
end

-- Activate an installed tuning sink through the A/B session: ab-switch first
-- (works when a session is already active), else ab-start then ab-switch.
local function activateTuning(sinkName)
  runBackendStream({ "ab-switch", "--name", sinkName }, function(swData, err)
    if err then
      if tostring(err):find("session", 1, true) == nil then
        fail("ab-switch", err)
        return
      end
      -- No session yet: start one (the backend auto-detects the raw sink when
      -- unambiguous), then switch to the requested tuning sink.
      runBackendStream({ "ab-start", "--prefix", sinkPrefix },
        function(_, err2)
          if err2 then
            fail("ab-start", err2)
            return
          end
          runBackendStream({ "ab-switch", "--name", sinkName },
            function(sw2, err3)
              if err3 then
                fail("ab-switch", err3)
                return
              end
              if sw2 == nil then
                return -- reported unavailable
              end
              activeTuning = sinkName
              lastTuning = sinkName
              publish("active_tuning", sinkName)
              publish("last_applied", { id = sinkName, at = os.time() })
              publish("ab_state", { active = true, data = sw2 })
              notify("notify.applied", { tuning = sinkName })
            end, "ab-switch", true)
        end, "ab-start", true)
      return
    end
    if swData == nil then
      return -- reported unavailable
    end
    activeTuning = sinkName
    lastTuning = sinkName
    publish("active_tuning", sinkName)
    publish("last_applied", { id = sinkName, at = os.time() })
    publish("ab_state", { active = true, data = swData })
    notify("notify.applied", { tuning = sinkName })
  end, "ab-switch", true)
end

local function toggleTuning()
  if activeTuning ~= nil then
    -- Bypass: end the A/B session (full restore), remember the tuning.
    runBackendStream({ "ab-stop" }, function(data, err)
      if err then
        fail("ab-stop", err)
        return
      end
      if data == nil then
        return
      end
      activeTuning = nil
      publish("active_tuning", nil)
      publish("ab_state", { active = false, data = data })
      publish("apply_result", { id = lastTuning, bypassed = true, data = data })
    end, "ab-stop", true)
  elseif lastTuning ~= nil then
    activateTuning(lastTuning)
  else
    fail("toggle", noctalia.tr("error.no_tuning"))
  end
end

-- Step to the next A/B candidate. `dir` is accepted for IPC compatibility;
-- ab-switch without arguments advances to the next candidate.
local function cycleTuning(dir)
  if activeTuning == nil then
    if #tunings == 0 then
      refreshTunings()
      return
    end
    activateTuning(tuningSinkName(tunings[1]))
    return
  end
  runBackendStream({ "ab-switch" }, function(data, err)
    if err then
      fail("ab-switch", err)
      return
    end
    if data == nil then
      return
    end
    activeTuning = data.selected
    publish("active_tuning", activeTuning)
    publish("ab_state", { active = true, data = data })
  end, "ab-switch", true)
end

-- ── measurement wizard ──────────────────────────────────────────────────────
-- probe → capture (reference) → analyse --out ref.txt
--   [user changes configuration — e.g. applies a candidate tuning]
-- → capture (candidate) → analyse --out raw.txt
-- → delta raw.txt ref.txt --out target.txt
-- → fit target.txt --rate <capture rate> --out fit.txt
-- → generate fit.txt --prefix <prefix> --out filter-chain.conf
--   [panel offers the backend-default dry-run apply, then the confirmed
--    apply --write --sink <sink>]
--
-- Per the contracts: analyse writes the response as <freq> <dbfs> text and
-- delta writes the target as <freq> <db> text, both via --out FILE (absolute
-- path under "out"). SA2's `fit` takes exactly ONE positional TARGET.txt (the
-- delta output) — never two files — and `generate` takes a positional FIT with
-- --prefix; the @SPEAKER_SINK@ placeholder is substituted at apply time.
-- Absent sibling commands surface via the watchdog after ~5 min of stream
-- silence (a missing command writes only usage to stderr, never an envelope).

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
      runBackend({ "delta", s.raw_txt, s.ref_txt,
                   "--out", s.dir .. "/target.txt" }, function(deltaData)
        if deltaData == nil then
          wizardPublish("failed", { step = "delta" })
          return
        end
        s.delta = deltaData
        s.target_txt = deltaData.out or (s.dir .. "/target.txt")
        wizardPublish("measuring", { step = "fit" })
        -- SA2's `fit` takes exactly ONE positional TARGET.txt (the delta
        -- output materialized above) and the capture's actual rate — never
        -- two files, never a hardcoded 48000.
        local rate = s.probe and s.probe.rate
        local fitArgs = { "fit", s.target_txt, "--out", s.dir .. "/fit.txt" }
        if rate ~= nil then
          table.insert(fitArgs, "--rate")
          table.insert(fitArgs, tostring(rate))
        end
        runBackendStream(fitArgs, function(fitData, err)
          if err then
            wizardPublish("failed", { step = "fit" })
            return
          end
          if fitData == nil then
            return -- reported unavailable
          end
          s.fit = fitData
          local fitPath = fitData.out or fitData.fit or fitData.path
          if fitPath == nil then
            wizardPublish("failed", { step = "generate", msg = "fit returned no fit path" })
            return
          end
          wizardPublish("measuring", { step = "generate" })
          runBackendStream({
            "generate", fitPath, "--prefix", sinkPrefix,
            "--out", s.dir .. "/filter-chain.conf",
          }, function(genData, err)
            if err then
              wizardPublish("failed", { step = "generate" })
              return
            end
            if genData == nil then
              return -- reported unavailable
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
    if req.config ~= nil then
      -- Generated conf: the backend's dry-run is the default; the confirmed
      -- panel step re-calls with dry_run=false -> --write + --sink.
      installConfig(req.config, req.dry_run ~= true)
    else
      -- Installed tuning: activate through the A/B session.
      activateTuning(tuningSinkName(req.id))
    end
  elseif action == "ab" then
    local op = (req and req.op) or "switch"
    if op == "start" then
      local sink = req and req.sink
      if not sink then
        fail("ab-start", noctalia.tr("error.no_sink"))
        return
      end
      runBackendStream({ "ab-start", "--prefix", sinkPrefix, "--sink", sink },
        function(data, err)
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
      runBackendStream({ "ab-stop" }, function(data, err)
        if err or data == nil then
          if not err then
            return
          end
          fail("ab-stop", err)
          return
        end
        activeTuning = nil
        publish("ab_state", { active = false, data = data })
      end, "ab-stop", true)
    else
      runBackendStream({ "ab-switch" }, function(data, err)
        if err or data == nil then
          if not err then
            return
          end
          fail("ab-switch", err)
          return
        end
        activeTuning = data.selected
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
  -- Watchdog: runStream has no completion callback at API 9. A child that
  -- dies without a done event or an error envelope (killed mid-run, or a
  -- missing command writing only to stderr) would leave busy set forever.
  -- Runs BEFORE the busy-skip below so it is never starved.
  watchdogTick = watchdogTick + 1
  if busy and lastStreamTick ~= nil
      and (watchdogTick - lastStreamTick) > STREAM_WATCHDOG_TICKS then
    streamDead = true
    lastStreamTick = nil
    fail(busyScope or "stream", "stream produced no output for "
        .. (STREAM_WATCHDOG_TICKS * 30) .. "s (watchdog)")
  end
  -- Idle cadence: keep the device list fresh; skip while a command runs.
  if not busy then
    refreshDevices()
  end
end
