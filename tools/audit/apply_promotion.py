import os
import shutil
import subprocess
from datetime import datetime

env_path = "/opt/signalcopyreal/.env"
now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
bak_path = f"/opt/signalcopyreal/.env.bak_promote_{now_str}"

shutil.copy2(env_path, bak_path)
print(f"Backup created at: {bak_path}")

to_remove_from_calib = {"-1001855648946", "-1001561851861", "-1001756316676"}
bonus_to_add = ["-1001561851861:5", "-1001756316676:5", "-1001855648946:4"]

with open(env_path, "r") as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    stripped = line.strip()
    if stripped.startswith("SIGNAL_COPY_CALIBRATION_CHANNELS="):
        val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
        cids = [c.strip() for c in val.split(",") if c.strip()]
        filtered_cids = [c for c in cids if c not in to_remove_from_calib]
        new_val = ",".join(filtered_cids)
        new_lines.append(f"SIGNAL_COPY_CALIBRATION_CHANNELS={new_val}\n")
        print(f"Updated SIGNAL_COPY_CALIBRATION_CHANNELS:")
        print(f"  Old: {val}")
        print(f"  New: {new_val}")
    elif stripped.startswith("SIGNAL_COPY_CHANNEL_SCORE_BONUS="):
        val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
        bonuses = [b.strip() for b in val.split(",") if b.strip()]
        for b in bonus_to_add:
            key = b.split(":")[0]
            # remove old if already there
            bonuses = [x for x in bonuses if not x.startswith(key + ":")]
            bonuses.append(b)
        new_val = ",".join(bonuses)
        new_lines.append(f"SIGNAL_COPY_CHANNEL_SCORE_BONUS={new_val}\n")
        print(f"Updated SIGNAL_COPY_CHANNEL_SCORE_BONUS:")
        print(f"  New: {new_val}")
    else:
        new_lines.append(line)

with open(env_path, "w") as f:
    f.writelines(new_lines)

print("Saved new /opt/signalcopyreal/.env")

# Recreate/restart container
print("Restarting nexus_signal_copy with docker compose...")
res = subprocess.run(
    ["docker", "compose", "-f", "/opt/signalcopyreal/docker-compose.yml", "up", "-d", "--no-deps", "signal_copy"],
    capture_output=True,
    text=True,
    cwd="/opt/signalcopyreal"
)
print("STDOUT:", res.stdout)
if res.stderr:
    print("STDERR:", res.stderr)

# Verify container env
verify = subprocess.run(
    ["docker", "exec", "nexus_signal_copy", "env"],
    capture_output=True,
    text=True
)
for l in verify.stdout.splitlines():
    if "CALIBRATION_CHANNELS" in l or "SCORE_BONUS" in l:
        print("CONTAINER ENV VERIFIED:", l)
