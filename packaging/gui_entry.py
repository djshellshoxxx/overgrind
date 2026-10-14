"""PyInstaller entry for the Overgrind GUI (freeze_support is required for the spawned worker)."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from overgrind.gui.app import main
    raise SystemExit(main())
