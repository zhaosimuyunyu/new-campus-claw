import sys,json,statistics
from pathlib import Path
numbers=[float(x) for x in sys.argv[1:]]
result={"count":len(numbers),"mean":statistics.mean(numbers),"median":statistics.median(numbers)}
Path('outputs').mkdir(exist_ok=True)
Path('outputs/summary.json').write_text(json.dumps(result),encoding='utf-8')
print(json.dumps(result))
