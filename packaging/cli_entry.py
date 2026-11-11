"""PyInstaller entry for the Overgrind console tool."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from overgrind.__main__ import main
    raise SystemExit(main())
