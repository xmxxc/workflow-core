#!/usr/bin/env python3
"""wfc: the workflow dispatcher's single command.

  wfc dispatch | done | ask | reply | continue | nudge | resume | status | wait | relay
  wfc --version

`build.py` packs this file and router.py into one executable file. The controller skill that
drives it, super-coding, is published separately in xmxxc/super-coding.
"""

import sys

import router

VERSION = "0.4.0"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["--version"], ["-V"]):
        print(f"wfc {VERSION}")
        return 0
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    return router.main(argv)


if __name__ == "__main__":
    sys.exit(main())
