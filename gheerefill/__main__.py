import sys

if sys.version_info < (3, 9):  # keep this file free of newer syntax so the message is always shown
    sys.stderr.write("gheerefill needs Python >= 3.9 (this is %s); run `make setup` or set HARNESS_PYTHON.\n"
                     % sys.version.split()[0])
    sys.exit(2)

from gheerefill.cli import main  # noqa: E402

sys.exit(main())
