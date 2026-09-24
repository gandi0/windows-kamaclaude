from pathlib import Path
import sys
p=Path(sys.argv[1])
p.write_text(str(int(p.read_text())+1) if p.exists() else '1')
raise SystemExit(7)
