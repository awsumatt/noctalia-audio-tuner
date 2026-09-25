"""devices — read-only enumeration of sinks and sources.

Data shape is discovery.devices():
  {"sinks":[{name,index,description,kind,rate,channels,monitor_source,
             default,speaker_candidate,ports:[...]}],
   "sources":[{name,index,description,kind,monitor_of,default,ports:[...]}],
   "default_sink":str, "default_source":str, "speaker_sink":str|null}

Classification is property-based (media.class / api.alsa.* / port type) —
never a regex on vendor names.
"""
from .. import discovery
from ..progress import progress


def run(args):
    progress(args, 50, "querying PipeWire")
    data = discovery.devices()
    progress(args, 100, f"{len(data['sinks'])} sinks, "
                        f"{len(data['sources'])} sources")
    return data


def register(sub):
    p = sub.add_parser("devices", help="enumerate sinks and sources (read-only)")
    p.set_defaults(func=run)
