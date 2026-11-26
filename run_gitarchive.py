import sys

from gitarchive.app import main


if __name__ == "__main__":
    # The imports above load the full desktop and LAN/cryptography dependency
    # graph. This switch lets the Windows build verify the frozen bundle
    # without opening a GUI window.
    if "--self-test" in sys.argv[1:]:
        raise SystemExit(0)
    raise SystemExit(main())
