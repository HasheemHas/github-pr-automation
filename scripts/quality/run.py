"""Run only centrally configured validation commands; target code gets no secrets."""
import json, os, subprocess, sys, yaml
from pathlib import Path

cfg=yaml.safe_load(Path(os.environ['CONFIG_FILE']).read_text())['repositories'][os.environ['TARGET_REPOSITORY']]['commands']
results=[]
for group in ('install','lint','test','build'):
    for command in cfg.get(group) or []:
        code=subprocess.run(command, shell=True).returncode
        results.append({'group':group,'command':command,'passed':code==0})
Path(os.environ['QUALITY_FILE']).write_text(json.dumps(results,indent=2)+'\n')
if not all(x['passed'] for x in results): sys.exit(1)
