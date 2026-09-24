from pathlib import Path
counter = Path('counter.txt')
value = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0
counter.write_text(str(value + 1), encoding='utf-8')
Path('marker.txt').write_text('incremented', encoding='utf-8')
