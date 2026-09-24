import pathlib, sys
pathlib.Path(sys.argv[1]).write_text('changed')
