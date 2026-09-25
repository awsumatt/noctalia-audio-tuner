"""generate — emit installable configs from a fit result.

  generate [--target {pipewire,omarchy,easyeffects}] [--prefix STR]
           [--sink STR] [--out-dir DIR] [--out FILE] FIT

FIT is a path to an upstream fit.txt (as `fit --out` or fit-eq.py writes it)
or to a saved fit-result JSON payload (the `fit` command's data object, or the
full envelope). Default target: pipewire.

Data (target-dependent):
  pipewire:     {"conf", "prefix", "sink", "n_sections", "linear_gain", "out"}
  omarchy:      writes <out-dir>/filter-chain.conf + <out-dir>/tuning.conf;
                data = {"out_dir","filter_chain_conf","tuning_conf","prefix",
                "sink","n_sections","linear_gain","magnitude_rms_db",
                "bass_group_delay_swing_ms","conf","tuning_conf_text"}
                (requires the JSON payload; a bare fit.txt has no
                magnitude_rms_db to report)
  easyeffects:  {"preset", "out", "n_sections", "global_gain_db"}
"""
import json
import os

from .. import TunerError, export as exportmod


def run(args):
    target = args.target
    if target == "pipewire":
        res = exportmod.export_pipewire(args.fit, args.prefix, args.sink)
        out = None
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w") as fh:
                fh.write(res["conf"] + "\n")
            out = os.path.abspath(args.out)
        res["out"] = out
        return res
    if target == "omarchy":
        if not args.out_dir:
            raise TunerError("no_out_dir",
                             "--target omarchy requires --out-dir DIR")
        return exportmod.export_omarchy(args.fit, args.prefix, args.sink,
                                        args.out_dir)
    if target == "easyeffects":
        return exportmod.export_easyeffects(args.fit, out_path=args.out)
    raise TunerError("bad_target", f"unknown export target: {target}")


def register(sub):
    p = sub.add_parser("generate", help="emit a filter-chain.conf / tuning "
                                        "tree / EasyEffects preset from a fit")
    p.add_argument("fit", help="path to a fit.txt or a saved fit-result JSON")
    p.add_argument("--target", choices=["pipewire", "omarchy", "easyeffects"],
                   default="pipewire")
    p.add_argument("--prefix", default="noctalia_audio_tuning",
                   help="filter-chain node name prefix (default "
                        "noctalia_audio_tuning)")
    p.add_argument("--sink", default="@SPEAKER_SINK@",
                   help="target sink (default: the @SPEAKER_SINK@ placeholder, "
                        "substituted at install time)")
    p.add_argument("--out-dir", help="output directory (required for "
                                     "--target omarchy)")
    p.add_argument("--out", help="write the artifact to this file "
                                 "(pipewire conf / easyeffects preset)")
    p.set_defaults(func=run)
